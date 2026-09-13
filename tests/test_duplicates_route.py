"""Tests for the duplicate-.feedpak HTTP routes (routes.py, issue #49):
GET /api/plugins/feedpakr/duplicates and POST .../duplicates/delete.

Reuses the _FakeApp harness pattern already established in
test_manual_offset_route.py / test_upgrade_conflict_route.py, driving the
real feedpakr_dedupe sibling (via a real load_sibling) against files on
disk rather than mocking it out.
"""

import asyncio
import importlib
import sys
import types
import zipfile
from pathlib import Path


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


def _build_app(monkeypatch, dlc_dir):
    routes = _load_routes(monkeypatch)
    app = _FakeApp()
    routes.setup(app, _context(dlc_dir))
    return app


def _write_pack(path: Path, members: dict, date_time=(2024, 1, 1, 0, 0, 0)) -> None:
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=date_time)
            zf.writestr(info, data)


# ── GET /duplicates ──────────────────────────────────────────────────────

def test_list_duplicates_reports_groups(tmp_path, monkeypatch):
    members = {'manifest.yaml': b'title: Song\n'}
    _write_pack(tmp_path / 'Song.feedpak', members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(tmp_path / 'Song_2.feedpak', members, date_time=(2024, 2, 1, 0, 0, 0))

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('GET', '/api/plugins/feedpakr/duplicates')]

    result = asyncio.run(handler())

    assert 'error' not in result
    assert len(result['groups']) == 1
    paths = {f['path'] for f in result['groups'][0]['files']}
    assert paths == {'Song.feedpak', 'Song_2.feedpak'}
    assert result['trash_dir'] == '.feedpakr_trash'


def test_list_duplicates_requires_dlc_configured(monkeypatch):
    app = _build_app(monkeypatch, None)
    handler = app.handlers[('GET', '/api/plugins/feedpakr/duplicates')]

    result = asyncio.run(handler())

    assert result == {'error': 'DLC folder not configured'}


def test_list_duplicates_empty_dlc_reports_no_groups(tmp_path, monkeypatch):
    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('GET', '/api/plugins/feedpakr/duplicates')]

    result = asyncio.run(handler())

    assert result['groups'] == []


# ── POST /duplicates/delete ──────────────────────────────────────────────

def test_delete_route_requires_confirm_flag(tmp_path, monkeypatch):
    members = {'manifest.yaml': b'title: Song\n'}
    _write_pack(tmp_path / 'Song.feedpak', members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(tmp_path / 'Song_2.feedpak', members, date_time=(2024, 2, 1, 0, 0, 0))

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('POST', '/api/plugins/feedpakr/duplicates/delete')]

    result = asyncio.run(handler({'paths': ['Song_2.feedpak']}))  # no confirm

    assert 'error' in result
    assert (tmp_path / 'Song_2.feedpak').exists()  # untouched


def test_delete_route_requires_dlc_configured(monkeypatch):
    app = _build_app(monkeypatch, None)
    handler = app.handlers[('POST', '/api/plugins/feedpakr/duplicates/delete')]

    result = asyncio.run(handler({'paths': ['Song_2.feedpak'], 'confirm': True}))

    assert result == {'error': 'DLC folder not configured'}


def test_delete_route_rejects_malformed_paths_payload(tmp_path, monkeypatch):
    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('POST', '/api/plugins/feedpakr/duplicates/delete')]

    assert 'error' in asyncio.run(handler({'confirm': True}))  # no paths key
    assert 'error' in asyncio.run(handler({'confirm': True, 'paths': []}))  # empty
    assert 'error' in asyncio.run(handler({'confirm': True, 'paths': 'not-a-list'}))
    assert 'error' in asyncio.run(handler({'confirm': True, 'paths': [123]}))  # not strings


def test_delete_route_confirmed_moves_selected_file_to_trash(tmp_path, monkeypatch):
    members = {'manifest.yaml': b'title: Song\n'}
    _write_pack(tmp_path / 'Song.feedpak', members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(tmp_path / 'Song_2.feedpak', members, date_time=(2024, 2, 1, 0, 0, 0))

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('POST', '/api/plugins/feedpakr/duplicates/delete')]

    result = asyncio.run(handler({'paths': ['Song_2.feedpak'], 'confirm': True}))

    assert not (tmp_path / 'Song_2.feedpak').exists()
    assert (tmp_path / 'Song.feedpak').exists()
    assert 'trashed_to' in result['results'][0]
    trashed = list((tmp_path / '.feedpakr_trash').iterdir())
    assert len(trashed) == 1


def test_delete_route_confirmed_still_refuses_sloppak_path(tmp_path, monkeypatch):
    """Route-level confirm:true must not bypass feedpakr_dedupe's own
    never-touch-.sloppak guard — the safety lives in the dedupe module,
    not the route, and this exercises that end to end."""
    sloppak = tmp_path / 'Song.sloppak'
    sloppak.write_bytes(b'must never be touched')

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('POST', '/api/plugins/feedpakr/duplicates/delete')]

    result = asyncio.run(handler({'paths': ['Song.sloppak'], 'confirm': True}))

    assert sloppak.exists()
    assert sloppak.read_bytes() == b'must never be touched'
    assert 'error' in result['results'][0]


# ── ws_upgrade: .sloppak-only path validation (issue #49) ────────────────

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


def test_ws_upgrade_rejects_non_sloppak_paths(tmp_path, monkeypatch):
    feedpak = tmp_path / 'Song.feedpak'
    feedpak.write_bytes(b'not actually relevant, request is rejected first')

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('WS', '/ws/plugins/feedpakr/upgrade')]
    ws = _FakeWebSocket()

    asyncio.run(handler(ws, paths='Song.feedpak'))

    assert ws.closed
    assert len(ws.sent) == 1
    assert 'Song.feedpak' in ws.sent[0]['error']
    assert '.sloppak' in ws.sent[0]['error']


def test_ws_upgrade_rejects_batch_with_any_non_sloppak_path(tmp_path, monkeypatch):
    """A mixed batch (one real .sloppak, one bogus path) must reject the
    whole request up front rather than silently processing the valid one
    and erroring on the other — matches the audio_mode/tracks validation
    style already used elsewhere in ws_build."""
    import yaml
    src = tmp_path / 'song.sloppak'
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump({
            'title': 'song', 'artist': 'A', 'duration': 10.0,
            'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
            'arrangements': [],
        }))

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('WS', '/ws/plugins/feedpakr/upgrade')]
    ws = _FakeWebSocket()

    asyncio.run(handler(ws, paths='song.sloppak,Other.feedpak'))

    assert ws.closed
    assert 'Other.feedpak' in ws.sent[0]['error']
    # Nothing should have been converted — the whole batch was rejected.
    assert not (tmp_path / 'song.feedpak').exists()


def test_ws_upgrade_accepts_sloppak_extension_case_insensitively(tmp_path, monkeypatch):
    import yaml
    src = tmp_path / 'song.SLOPPAK'
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump({
            'title': 'song', 'artist': 'A', 'duration': 10.0,
            'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
            'arrangements': [],
        }))

    app = _build_app(monkeypatch, str(tmp_path))
    handler = app.handlers[('WS', '/ws/plugins/feedpakr/upgrade')]
    ws = _FakeWebSocket()

    asyncio.run(handler(ws, paths='song.SLOPPAK', conflict_policy='versioned'))

    done = [m for m in ws.sent if m.get('done')]
    assert done, ws.sent
    assert 'error' not in done[0]
