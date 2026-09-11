"""Tests for ws_upgrade's conflict_policy handling (routes.py, issue #49).

Reuses the _FakeApp/_FakeWebSocket harness pattern from
test_manual_offset_route.py, but drives the real feedpakr_upgrade /
feedpakr_pack sibling modules (via a real load_sibling) so the write-path
behavior — skip / replace / numbered copy — is exercised end to end
against real files on disk, not mocked out.
"""

import asyncio
import importlib
import io
import json
import sys
import types
import zipfile
from pathlib import Path

import yaml


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


def _real_load_sibling(name):
    return importlib.import_module(name)


def _context(dlc_dir):
    return {
        'get_dlc_dir': lambda: dlc_dir,
        'extract_meta': lambda path: {},
        'meta_db': types.SimpleNamespace(put=lambda *args, **kwargs: None),
        'log': types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
        'load_sibling': _real_load_sibling,
    }


def _build_handler(monkeypatch, dlc_dir):
    routes = _load_routes(monkeypatch)
    app = _FakeApp()
    routes.setup(app, _context(dlc_dir))
    return app.handlers[('WS', '/ws/plugins/feedpakr/upgrade')]


def _write_sloppak(dlc_root: Path, name: str) -> Path:
    """Minimal zip-form sloppak (matches how the DLC folder actually stores
    them — ws_upgrade requires src_path.is_file(), so a dir-form sloppak
    would be rejected before ever reaching the conflict-policy logic)."""
    src = dlc_root / f'{name}.sloppak'
    manifest = {
        'title': name, 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
        'arrangements': [],
    }
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(manifest, sort_keys=False))
    return src


async def _run_upgrade(handler, ws, **kwargs):
    """ws_upgrade streams progress via an internal queue and only resolves
    once a background executor task finishes; call the handler and drain
    every message it sends onto `ws` (mirrors what a real client sees)."""
    await handler(ws, **kwargs)


def test_conflict_policy_skip_leaves_existing_feedpak_untouched(tmp_path, monkeypatch):
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')
    existing = dlc_root / 'song.feedpak'
    existing.write_bytes(b'sentinel-bytes-must-survive')

    handler = _build_handler(monkeypatch, str(dlc_root))
    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak', conflict_policy='skip'))

    assert existing.read_bytes() == b'sentinel-bytes-must-survive'
    done = [m for m in ws.sent if m.get('done')]
    assert done, ws.sent
    results = done[0]['results']
    assert results == [{'path': 'song.sloppak', 'skipped': 'already_upgraded', 'output_rel': 'song.feedpak'}]
    # No numbered copy should have been created either.
    assert not (dlc_root / 'song_2.feedpak').exists()


def test_conflict_policy_replace_overwrites_existing_feedpak(tmp_path, monkeypatch):
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')
    existing = dlc_root / 'song.feedpak'
    existing.write_bytes(b'stale-bytes')

    handler = _build_handler(monkeypatch, str(dlc_root))
    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak', conflict_policy='replace'))

    assert existing.exists()
    assert existing.read_bytes() != b'stale-bytes'  # overwritten with the fresh conversion
    assert not (dlc_root / 'song_2.feedpak').exists()
    done = [m for m in ws.sent if m.get('done')]
    result = done[0]['results'][0]
    assert result['output'] == 'song.feedpak'


def test_conflict_policy_versioned_keeps_existing_and_adds_numbered_copy(tmp_path, monkeypatch):
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')
    existing = dlc_root / 'song.feedpak'
    existing.write_bytes(b'original-bytes')

    handler = _build_handler(monkeypatch, str(dlc_root))
    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak', conflict_policy='versioned'))

    assert existing.read_bytes() == b'original-bytes'  # untouched
    assert (dlc_root / 'song_2.feedpak').exists()
    done = [m for m in ws.sent if m.get('done')]
    result = done[0]['results'][0]
    assert result['output'] == 'song_2.feedpak'


def test_default_conflict_policy_matches_pre_49_versioned_behavior(tmp_path, monkeypatch):
    """No conflict_policy passed at all (an older/unmodified client) must
    behave exactly as before this fix — never silently skip or replace."""
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')
    existing = dlc_root / 'song.feedpak'
    existing.write_bytes(b'original-bytes')

    handler = _build_handler(monkeypatch, str(dlc_root))
    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak'))

    assert existing.read_bytes() == b'original-bytes'
    assert (dlc_root / 'song_2.feedpak').exists()


def test_unrecognized_conflict_policy_falls_back_to_versioned(tmp_path, monkeypatch):
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')
    existing = dlc_root / 'song.feedpak'
    existing.write_bytes(b'original-bytes')

    handler = _build_handler(monkeypatch, str(dlc_root))
    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak', conflict_policy='bogus'))

    assert existing.read_bytes() == b'original-bytes'
    assert (dlc_root / 'song_2.feedpak').exists()


def test_no_conflict_writes_directly_regardless_of_policy(tmp_path, monkeypatch):
    """When there's no existing .feedpak, conflict_policy is moot — the
    output should land at the clean, non-numbered name either way."""
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')

    handler = _build_handler(monkeypatch, str(dlc_root))
    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak', conflict_policy='skip'))

    out = dlc_root / 'song.feedpak'
    assert out.exists()
    done = [m for m in ws.sent if m.get('done')]
    result = done[0]['results'][0]
    assert result['output'] == 'song.feedpak'
    assert 'skipped' not in result


def test_replace_write_failure_does_not_truncate_the_existing_feedpak(tmp_path, monkeypatch):
    """Sourcery review finding on PR #61: a plain write_bytes() truncates
    the target before writing, so a disk-full/interrupted write under
    conflict_policy='replace' used to leave a previously-valid .feedpak
    empty or partial. _atomic_write_bytes() must write to a temp file and
    only os.replace() it in on full success — simulate a mid-write failure
    and assert the original bytes survive untouched."""
    dlc_root = tmp_path
    src = _write_sloppak(dlc_root, 'song')
    existing = dlc_root / 'song.feedpak'
    existing.write_bytes(b'original-bytes-must-survive')

    handler = _build_handler(monkeypatch, str(dlc_root))
    routes_mod = sys.modules['routes']  # must grab the module _build_handler just (re)imported, not a stale reference

    def _boom(path, data):
        raise OSError('simulated disk-full mid-write')

    monkeypatch.setattr(routes_mod, '_atomic_write_bytes', _boom)

    ws = _FakeWebSocket()
    asyncio.run(_run_upgrade(handler, ws, paths='song.sloppak', conflict_policy='replace'))

    assert existing.read_bytes() == b'original-bytes-must-survive'
    # No stray temp file left behind under _atomic_write_bytes's own
    # cleanup path either (this monkeypatch bypasses that, so this just
    # confirms nothing else in the handler wrote a sibling artifact).
    assert list(dlc_root.glob('*.tmp')) == []
    done = [m for m in ws.sent if m.get('done')]
    result = done[0]['results'][0]
    assert result['error'] == 'simulated disk-full mid-write'


def test_atomic_write_bytes_cleans_up_temp_file_on_real_failure(tmp_path, monkeypatch):
    """Drives _atomic_write_bytes directly (not through the route) so the
    temp-file cleanup path in its own except block is exercised for real —
    fail the final os.replace() (the swap-in step) and confirm neither the
    original file's bytes nor a stray temp file survive."""
    _build_handler(monkeypatch, str(tmp_path))  # ensures routes is importable (fastapi isn't installed here)
    routes_mod = sys.modules['routes']

    target = tmp_path / 'song.feedpak'
    target.write_bytes(b'original-bytes')

    def _boom_replace(_src, _dst):
        raise OSError('simulated replace failure')

    monkeypatch.setattr(routes_mod.os, 'replace', _boom_replace)

    try:
        routes_mod._atomic_write_bytes(target, b'new-bytes')
    except OSError:
        pass

    assert target.read_bytes() == b'original-bytes'
    assert list(tmp_path.glob('.*.tmp')) == []


def test_atomic_write_bytes_restores_umask_derived_permissions(tmp_path, monkeypatch):
    """pullfrog review, PR #61: mkstemp() creates its file 0600 regardless
    of the process umask, unlike the plain open(path, 'wb') it replaces —
    without the chmod, a re-upgraded .feedpak would silently become
    unreadable to anyone but the owning process."""
    import stat

    _build_handler(monkeypatch, str(tmp_path))
    routes_mod = sys.modules['routes']

    target = tmp_path / 'song.feedpak'
    routes_mod._atomic_write_bytes(target, b'new-bytes')

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o644
