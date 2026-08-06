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
