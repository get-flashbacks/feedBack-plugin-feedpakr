"""Tests for ws_build's manual_offset validation (routes.py).

Reuses the _FakeApp/_load_routes/_context harness pattern already
established in test_handoffs_route.py so ws_build's plain-Python handler
can be invoked directly without a real ASGI server. Fixture-free — unlike
most of test_pipeline.py, these run wherever the repo itself is importable.
"""

import asyncio
import importlib
import sys
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


def _context():
    def load_sibling(name):
        mod = types.SimpleNamespace()
        if name == 'feedpakr_pipeline':
            mod.configure_sibling_loader = lambda loader: None
        return mod

    return {
        # None here is deliberate: it makes ws_build fail fast at its own
        # "DLC folder not configured" check right after the manual_offset
        # guard, which is exactly what these tests want to observe next.
        'get_dlc_dir': lambda: None,
        'extract_meta': lambda path: {},
        'meta_db': types.SimpleNamespace(put=lambda *args, **kwargs: None),
        'log': types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
        'load_sibling': load_sibling,
    }


def _build_handler(monkeypatch):
    routes = _load_routes(monkeypatch)
    app = _FakeApp()
    routes.setup(app, _context())
    return app.handlers[('WS', '/ws/plugins/feedpakr/build')]


def test_ws_build_rejects_non_finite_manual_offset(monkeypatch):
    """float() happily parses 'inf'/'Infinity'/'nan'/'1e999' — all of these
    must be rejected with a clean error, not silently flow into the chart
    as a literal Infinity/NaN timestamp (feedpakr PR #45 review)."""
    handler = _build_handler(monkeypatch)

    for bad in ('Infinity', 'inf', '-Infinity', 'NaN', '1e999'):
        ws = _FakeWebSocket()
        asyncio.run(handler(ws, upload_id='x', manual_offset=bad))
        assert ws.accepted
        assert ws.closed
        assert len(ws.sent) == 1
        assert 'manual_offset' in ws.sent[0]['error']
        assert 'finite' in ws.sent[0]['error']


def test_ws_build_rejects_unparseable_manual_offset(monkeypatch):
    handler = _build_handler(monkeypatch)
    ws = _FakeWebSocket()

    asyncio.run(handler(ws, upload_id='x', manual_offset='not-a-number'))

    assert ws.closed
    assert 'manual_offset' in ws.sent[0]['error']


def test_ws_build_accepts_valid_manual_offset(monkeypatch):
    """A genuine numeric manual_offset must clear the finite-offset guard
    and reach the next validation stage (DLC folder configured) instead —
    proving the guard doesn't false-positive on legitimate input.
    audio_mode='none' skips the gp2midi/gp8_audio_sync/ffmpeg dependency
    checks that would otherwise short-circuit before the DLC check."""
    handler = _build_handler(monkeypatch)
    ws = _FakeWebSocket()

    asyncio.run(handler(
        ws, upload_id='does-not-exist', tracks='0', audio_mode='none', manual_offset='2.5',
    ))

    assert ws.sent
    assert 'manual_offset' not in ws.sent[0]['error']
    assert ws.sent[0]['error'] == 'DLC folder not configured'


def test_ws_build_empty_manual_offset_means_auto_detect(monkeypatch):
    """The default '' must not trip the guard at all — it means 'auto-
    detect via autosync', not 'zero seconds'."""
    handler = _build_handler(monkeypatch)
    ws = _FakeWebSocket()

    asyncio.run(handler(ws, upload_id='does-not-exist', tracks='0', audio_mode='none'))

    assert ws.sent
    assert 'manual_offset' not in ws.sent[0]['error']
