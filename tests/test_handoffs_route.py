import asyncio
import base64
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


def test_handoffs_route_reports_registered_sibling_endpoints(monkeypatch):
    routes = _load_routes(monkeypatch)
    app = _FakeApp()
    app.routes.extend([
        _Route('/api/plugins/song_preview/backfill', {'POST'}),
        _Route('/api/plugins/stem_splitter/split', {'POST'}),
    ])

    routes.setup(app, _context())

    handler = app.handlers[('GET', '/api/plugins/feedpakr/handoffs')]
    assert asyncio.run(handler()) == {
        'handoffs': {'preview': True, 'split': True},
    }


def test_handoffs_route_ignores_auxiliary_probe_endpoints(monkeypatch):
    routes = _load_routes(monkeypatch)
    app = _FakeApp()
    app.routes.extend([
        _Route('/api/plugins/song_preview/audit', {'GET'}),
        _Route('/api/plugins/stem_splitter/jobs', {'GET'}),
    ])

    routes.setup(app, _context())

    handler = app.handlers[('GET', '/api/plugins/feedpakr/handoffs')]
    assert asyncio.run(handler()) == {
        'handoffs': {'preview': False, 'split': False},
    }


def test_handoffs_route_reports_missing_sibling_endpoints(monkeypatch):
    routes = _load_routes(monkeypatch)
    app = _FakeApp()

    routes.setup(app, _context())

    handler = app.handlers[('GET', '/api/plugins/feedpakr/handoffs')]
    assert asyncio.run(handler()) == {
        'handoffs': {'preview': False, 'split': False},
    }


def test_upload_gp_returns_error_on_parse_timeout(monkeypatch):
    """If parse_gp hangs beyond _PARSE_TIMEOUT_SECS the upload handler must
    return an error dict rather than blocking the event loop indefinitely."""
    routes = _load_routes(monkeypatch)

    def _slow_parse_gp(_path):
        time.sleep(0.2)  # longer than the patched 50 ms timeout
        return {}

    def load_sibling(name):
        mod = types.SimpleNamespace()
        if name == 'feedpakr_pipeline':
            mod.configure_sibling_loader = lambda loader: None
            mod.parse_gp = _slow_parse_gp
            mod.UnsupportedFormatError = Exception
        return mod

    ctx = {
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

    app = _FakeApp()
    routes.setup(app, ctx)

    # Shrink the timeout to 50 ms so the test completes quickly.
    monkeypatch.setattr(routes, '_PARSE_TIMEOUT_SECS', 0.05)

    handler = app.handlers[('POST', '/api/plugins/feedpakr/upload')]
    dummy_gp = base64.b64encode(b'\x00' * 16).decode()
    result = asyncio.run(handler({'filename': 'song.gp5', 'data': dummy_gp}))

    assert 'error' in result
    assert 'timed out' in result['error'].lower()

    # The timed-out dir must be registered as an upload entry with a deferred
    # TTL, so _purge_stale_uploads() won't rmtree it until the parse window
    # (plus grace) has elapsed and the executor thread has dropped its handles.
    entries = list(routes._uploads.values())
    assert len(entries) == 1
    assert time.monotonic() - entries[0]['ts'] < routes._UPLOAD_TTL_SECONDS
