"""Tests for ws_build's concurrent-output safety (routes.py, issue #74).

Two builds of the same sanitized title/artist run at once. Previously each
called unique_output_path's check-then-return and — since neither output
file existed yet — both selected the same name and the second write
silently clobbered the first. The build flow must reserve its output name
exclusively so both outputs survive under distinct names and the metadata
DB indexes each one consistently.

Reuses the _FakeApp/_FakeWebSocket harness pattern established in
test_manual_offset_route.py, but loads the real sibling modules so the
real reservation logic in feedpakr_pack runs against real files on disk.
"""

import asyncio
import importlib
import pathlib
import sys
import threading
import time
import types


class _Route:
    def __init__(self, path, methods):
        self.path = path
        self.methods = methods


class _FakeApp:
    def __init__(self):
        self.routes = []
        self.handlers = {}

    def get(self, path):
        def deco(fn):
            self.routes.append(_Route(path, {'GET'}))
            self.handlers[('GET', path)] = fn
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.routes.append(_Route(path, {'POST'}))
            self.handlers[('POST', path)] = fn
            return fn
        return deco

    def websocket(self, path):
        def deco(fn):
            self.handlers[('WS', path)] = fn
            return fn
        return deco


class _FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent = []
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


class _RecordingMetaDb:
    """meta_db stand-in that records every put() so the test can assert
    the DB sees exactly the two outputs that actually exist on disk."""

    def __init__(self):
        self.puts = []
        self._lock = threading.Lock()

    def put(self, rel_name, mtime, size, meta):
        with self._lock:
            self.puts.append((rel_name, mtime, size, meta))


def _load_routes(monkeypatch):
    fake_fastapi = types.ModuleType('fastapi')
    fake_fastapi.WebSocket = object
    fake_fastapi.WebSocketDisconnect = Exception
    fake_responses = types.ModuleType('fastapi.responses')
    fake_responses.FileResponse = object
    fake_responses.JSONResponse = lambda payload, status_code=200: {
        'payload': payload,
        'status_code': status_code,
    }
    monkeypatch.setitem(sys.modules, 'fastapi', fake_fastapi)
    monkeypatch.setitem(sys.modules, 'fastapi.responses', fake_responses)
    sys.modules.pop('routes', None)
    return importlib.import_module('routes')


def test_concurrent_builds_of_same_title_get_distinct_outputs(tmp_path, monkeypatch):
    routes = _load_routes(monkeypatch)
    dlc_dir = tmp_path / 'dlc'
    dlc_dir.mkdir()

    meta_db = _RecordingMetaDb()
    ctx = {
        'get_dlc_dir': lambda: str(dlc_dir),
        'extract_meta': lambda path: {'title': path.stem},
        'meta_db': meta_db,
        'log': types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
        'load_sibling': importlib.import_module,
    }
    app = _FakeApp()
    routes.setup(app, ctx)
    handler = app.handlers[('WS', '/ws/plugins/feedpakr/build')]

    # One barrier is deliberately reused for both rendezvous phases (inside
    # fake_build_feedpak, then again inside synchronized_write_bytes): each
    # of the two threads runs exactly one build and one publish, so the two
    # phases line up and the barrier stays in step. Both waits have timeouts;
    # if the phases ever desynchronized, Barrier raises BrokenBarrierError
    # instead of hanging.
    barrier = threading.Barrier(2, timeout=30)
    expected_payloads = {b'payload-build-a', b'payload-build-b'}
    payloads = list(expected_payloads)
    payload_lock = threading.Lock()

    def fake_build_feedpak(_gp_path, **kwargs):
        # Both executor threads release together straight into the output
        # path selection, so the pre-fix check-then-return race would fire.
        barrier.wait(timeout=30)
        with payload_lock:
            payload = payloads.pop(0)
        return {
            'bytes': payload,
            'title': 'Same',
            'artist': 'Artist',
            'arrangement_count': 1,
            'duration': 10.0,
            'warnings': [],
            'validation': [],
            'features': {'real_audio': False},
        }

    monkeypatch.setattr(routes._pipeline, 'build_feedpak', fake_build_feedpak)

    upload_dirs = []
    for i, token in enumerate(('tok-a', 'tok-b')):
        up = tmp_path / f'up{i}'
        up.mkdir()
        gp = up / 'tab.gp5'
        gp.write_bytes(b'')
        routes._uploads[token] = {
            'dir': str(up), 'gp_path': str(gp),
            'cover_path': None, 'audio_path': None,
            'existing_pack': None, 'ts': time.monotonic(),
        }
        upload_dirs.append(up)

    # Make the select-then-publish window deterministic: hold both builds at
    # the write so neither publishes before the other has selected its name.
    # With the pre-fix check-then-return logic both threads would pick the
    # same output and this barrier would let them clobber it; with the
    # exclusive reservation they hold distinct names, so the barrier passes
    # without ever touching the same file.
    original_write_bytes = pathlib.Path.write_bytes

    def synchronized_write_bytes(self, data):
        barrier.wait(timeout=30)
        return original_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, 'write_bytes', synchronized_write_bytes)

    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()

    async def _run_builds():
        await asyncio.gather(
            handler(ws1, upload_id='tok-a', tracks='0', audio_mode='none'),
            handler(ws2, upload_id='tok-b', tracks='0', audio_mode='none'),
        )

    asyncio.run(_run_builds())

    out_dir = dlc_dir / 'feedpakr'
    files = sorted(out_dir.glob('Same_Artist*.feedpak'), key=str)
    assert [p.name for p in files] == ['Same_Artist.feedpak', 'Same_Artist_2.feedpak']
    assert {p.read_bytes() for p in files} == expected_payloads
    assert list(out_dir.glob('.*.reserved')) == []

    # Consistent metadata indexing: every output on disk made it into the
    # DB, under its own distinct feedpakr-relative name.
    rel_names = {rel for rel, *_ in meta_db.puts}
    assert rel_names == {
        'feedpakr/Same_Artist.feedpak', 'feedpakr/Same_Artist_2.feedpak',
    }

    dones = [m for m in ws1.sent + ws2.sent if m.get('done')]
    assert len(dones) == 2
    assert {m['filename'] for m in dones} == {'Same_Artist.feedpak', 'Same_Artist_2.feedpak'}