"""Tests for ws_build's upload-token lifecycle (routes.py, issue #75).

ws_build used to pop the single-use upload token before parsing
`notation_tracks`. An invalid value returned its error only after the
token was gone, so the caller could not retry with the same upload and
its temp dir was orphaned — no longer registered in `_uploads`, the TTL
purger never visits it. Parameter validation must complete before the
token is consumed.

Reuses the _FakeApp/_FakeWebSocket harness from test_manual_offset_route.py
and loads the real sibling modules (so the build path uses the real
reservation/write logic) like test_build_concurrency_route.py.
"""

import asyncio
import importlib
import sys
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


def _build_handler(tmp_path, monkeypatch):
    routes = _load_routes(monkeypatch)
    dlc_dir = tmp_path / 'dlc'
    dlc_dir.mkdir()

    ctx = {
        'get_dlc_dir': lambda: str(dlc_dir),
        'extract_meta': lambda path: {'title': path.stem},
        'meta_db': types.SimpleNamespace(put=lambda *args, **kwargs: None),
        'log': types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
        'load_sibling': importlib.import_module,
    }
    app = _FakeApp()
    routes.setup(app, ctx)
    return routes, app.handlers[('WS', '/ws/plugins/feedpakr/build')]


def _register_upload(routes, tmp_path, token, i):
    up = tmp_path / f'up{i}'
    up.mkdir()
    gp = up / 'tab.gp5'
    gp.write_bytes(b'')
    routes._uploads[token] = {
        'dir': up, 'gp_path': gp,
        'cover_path': None, 'audio_path': None,
        'existing_pack': None, 'ts': time.monotonic(),
    }
    return up


def test_invalid_notation_tracks_preserves_retryable_upload(tmp_path, monkeypatch):
    """A bad notation_tracks value must be rejected while the token is
    still registered — otherwise the upload is unrecoverable and its temp
    dir is orphaned (issue #75)."""
    routes, handler = _build_handler(tmp_path, monkeypatch)
    up = _register_upload(routes, tmp_path, 'tok', 0)

    ws = _FakeWebSocket()
    asyncio.run(handler(
        ws, upload_id='tok', tracks='0', audio_mode='none', notation_tracks='bad',
    ))

    assert ws.accepted and ws.closed
    assert ws.sent[-1]['error'] == (
        'notation_tracks must be "default" or comma-separated integers'
    )

    # Still registered (so the caller can retry) and its dir is intact
    # (nothing was rmtree'd out from under a registered upload).
    assert 'tok' in routes._uploads
    assert up.exists()
    assert (up / 'tab.gp5').exists()


def test_upload_retryable_after_notation_tracks_validation_error(tmp_path, monkeypatch):
    """After the validation error the same token must still drive a real
    build — the failure is not allowed to burn the single-use upload."""
    routes, handler = _build_handler(tmp_path, monkeypatch)
    _register_upload(routes, tmp_path, 'tok', 0)

    def fake_build_feedpak(_gp_path, **kwargs):
        return {
            'bytes': b'payload',
            'title': 'Song',
            'artist': 'Artist',
            'arrangement_count': 1,
            'duration': 10.0,
            'warnings': [],
            'validation': [],
            'features': {'real_audio': False},
        }

    monkeypatch.setattr(routes._pipeline, 'build_feedpak', fake_build_feedpak)

    bad = _FakeWebSocket()
    asyncio.run(handler(
        bad, upload_id='tok', tracks='0', audio_mode='none', notation_tracks='bad',
    ))
    assert bad.sent[-1].get('error')

    retry = _FakeWebSocket()
    asyncio.run(handler(
        retry, upload_id='tok', tracks='0', audio_mode='none',
        notation_tracks='default',
    ))

    assert [m for m in retry.sent if m.get('done')], retry.sent
    assert 'tok' not in routes._uploads
