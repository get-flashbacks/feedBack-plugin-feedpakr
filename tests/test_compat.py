"""feedpakr's declared core-host compatibility (issue #66): the tested
baseline, the per-feature requirement separation, the runtime probes, and
the fallback behaviour feedpakr_upgrade now applies when a host's sloppak
module predates read_member_bytes()/load_manifest().
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import feedpakr_compat as compat
import feedpakr_upgrade as upgrade


# ── Baseline declaration ─────────────────────────────────────────────────

def test_baseline_is_pinned_by_exact_commit():
    assert len(compat.CORE_BASELINE_COMMIT) == 40
    assert compat.CORE_BASELINE_COMMIT_SHORT == compat.CORE_BASELINE_COMMIT[:7]
    assert compat.CORE_BASELINE_REPO


def test_features_covers_each_documented_operation_separately():
    # The issue asks for each operation to be documented and tested
    # independently — the feature keys ARE that split.
    assert set(compat.FEATURES) == {
        'gp_import',
        'existing_pack_reuse',
        'upgrade',
        'duplicate_scan',
        'output_playback',
    }


def test_output_playback_is_documented_not_probed():
    # Feedpakr writes the pack; whether the host can play it back is decided
    # by the host reader, so this is the one feature that is declared but not
    # probed at runtime.
    f = compat.FEATURES['output_playback']
    assert f.probed is False


# ── Separation of concerns (core vs external tools vs siblings) ───────────

def test_external_tools_are_declared_separately_from_core():
    for key, tool in compat.EXTERNAL_TOOLS.items():
        assert key == tool.label.lower().replace(' ', '-') or tool.binary or tool.module

    # A missing external binary is not a core-version problem: external-tool
    # names must never collide with — or be reported as — core modules.
    core_module_names = {
        m for f in compat.FEATURES.values()
        for m in (*f.core_modules, *f.optional_core_modules)
    }
    assert core_module_names.isdisjoint(compat.EXTERNAL_TOOLS)
    for feature in compat.FEATURES.values():
        assert set(feature.external_tools) <= set(compat.EXTERNAL_TOOLS)


def test_optional_siblings_are_declared_and_separate():
    for feature in compat.FEATURES.values():
        assert set(feature.optional_siblings) <= set(compat.OPTIONAL_SIBLINGS)
    # Siblings are plugin identity strings, never core module names.
    assert 'song' not in compat.OPTIONAL_SIBLINGS
    assert 'sloppak' not in compat.OPTIONAL_SIBLINGS


def test_core_helpers_refer_to_attributes_compat_probes():
    for feature in compat.FEATURES.values():
        for helper in feature.core_helpers:
            assert helper.key == f'{helper.module}.{helper.attr}'
            # Every helper must be one the runtime probe actually measures.
            assert helper in compat.FEATURES['upgrade'].core_helpers or \
                helper in compat.FEATURES['duplicate_scan'].core_helpers or \
                helper in compat.FEATURES['existing_pack_reuse'].core_helpers


def test_pack_read_helpers_all_have_fallbacks():
    # The whole point of the fallback work: a host missing read_member_bytes()
    # or load_manifest() must degrade to feedpakr's own reader, never fail the
    # operation. So no pack-read feature may be hard-unavailable on that basis.
    for key in ('upgrade', 'duplicate_scan', 'existing_pack_reuse'):
        for helper in compat.FEATURES[key].core_helpers:
            assert helper.has_fallback, f'{helper.key} is a hard requirement for {key}'


# ── Probes ─────────────────────────────────────────────────────────────────

class _FakeSloppak:
    def __init__(self, *, has_read_member_bytes=False, has_load_manifest=False):
        if has_read_member_bytes:
            self.read_member_bytes = lambda src, rel: b'host:' + rel.encode()
        if has_load_manifest:
            self.load_manifest = lambda src: {'title': 'from-host'}


def test_sloppak_missing_helpers():
    complete = _FakeSloppak(has_read_member_bytes=True, has_load_manifest=True)
    assert compat.sloppak_missing_helpers(complete) == []

    partial = _FakeSloppak(has_load_manifest=True)  # predates read_member_bytes
    assert compat.sloppak_missing_helpers(partial) == ['read_member_bytes']

    assert compat.sloppak_missing_helpers(None) == ['load_manifest', 'read_member_bytes']

    # has_helper is a real import + hasattr — an attribute set to None still
    # counts as present, matching the AttributeError this protects against.
    assert not compat.has_helper('feedpakr_compat', 'no_such_function')


def test_evaluate_feature_gp_import_available():
    result = compat.evaluate_feature(
        'gp_import', modules={'gp2rs': True, 'song': True}, helpers={},
    )
    assert result['status'] == 'available'
    assert result['ok'] is True
    assert result['missing_core_modules'] == []


def test_evaluate_feature_gp_import_unavailable_when_core_missing():
    result = compat.evaluate_feature(
        'gp_import', modules={'gp2rs': False, 'song': True}, helpers={},
    )
    assert result['status'] == 'unavailable'
    assert result['ok'] is False
    assert result['missing_core_modules'] == ['gp2rs']
    # The message must be actionable: name the gap and pin the baseline.
    assert 'gp2rs' in result['message']
    assert compat.CORE_BASELINE_COMMIT_SHORT in result['message']


def test_evaluate_feature_upgrade_degrades_when_host_predates_read_member_bytes():
    result = compat.evaluate_feature(
        'upgrade',
        modules={},
        helpers={
            'sloppak.load_manifest': True,
            'sloppak.read_member_bytes': False,
        },
    )
    assert result['status'] == 'degraded'
    assert result['ok'] is True
    assert [h['key'] for h in result['degraded_helpers']] == ['sloppak.read_member_bytes']
    assert 'fallback' in result['message']


def test_evaluate_feature_duplicate_scan_degrades_without_host_reader():
    result = compat.evaluate_feature('duplicate_scan', modules={}, helpers={})
    assert result['status'] == 'degraded'
    assert result['ok'] is True


def test_evaluate_feature_statuses_are_controlled():
    for key in compat.FEATURES:
        if not compat.FEATURES[key].probed:
            continue
        result = compat.evaluate_feature(key, modules={}, helpers={})
        assert result['status'] in ('available', 'degraded', 'unavailable')


def test_compatibility_report_shape():
    report = compat.compatibility_report()
    assert set(report['core_baseline']) == {
        'repo', 'commit', 'commit_short', 'version', 'note',
    }
    assert report['core_baseline']['commit'] == compat.CORE_BASELINE_COMMIT
    assert set(report['features']) == set(compat.FEATURES)
    assert set(report['external_tools']) == set(compat.EXTERNAL_TOOLS)
    assert isinstance(report['ok'], bool)
    for feature in report['features'].values():
        assert feature['status'] in ('available', 'degraded', 'unavailable', 'documented')


# ── The actual fallback bug (feedpakr_upgrade._read_member) ───────────────

def _pack_src(tmp_path) -> Path:
    src = tmp_path / 'song.sloppak'
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('manifest.yaml', json.dumps({'title': 'song', 'arrangements': []}))
        zf.writestr('arrangements/lead.json', b'{"tones": {"definitions": []}}')
    return src


def test_read_member_uses_host_reader_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, 'sloppak_mod', _FakeSloppak(
        has_read_member_bytes=True, has_load_manifest=True,
    ))
    src = _pack_src(tmp_path)
    assert upgrade._read_member(src, 'arrangements/lead.json') == b'host:arrangements/lead.json'
    assert upgrade.load_archive_manifest(src) == {'title': 'from-host'}


def test_read_member_falls_back_when_host_predates_read_member_bytes(tmp_path, monkeypatch):
    # Regression: an importable sloppak lacking read_member_bytes() used to
    # raise AttributeError instead of using the standalone reader.
    monkeypatch.setattr(upgrade, 'sloppak_mod', _FakeSloppak())  # neither helper
    src = _pack_src(tmp_path)
    assert upgrade._read_member(src, 'arrangements/lead.json') == b'{"tones": {"definitions": []}}'
    assert upgrade.load_archive_manifest(src) == {'title': 'song', 'arrangements': []}


def test_read_member_falls_back_with_no_sloppak_module_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, 'sloppak_mod', None)
    src = _pack_src(tmp_path)
    assert upgrade._read_member(src, 'arrangements/lead.json') == b'{"tones": {"definitions": []}}'
    # The dedupe path reads through the same guarded wrapper.
    assert upgrade.read_archive_member(src, 'arrangements/lead.json') == b'{"tones": {"definitions": []}}'


def test_read_member_fallback_still_rejects_unsafe_members(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, 'sloppak_mod', None)
    src = _pack_src(tmp_path)
    assert upgrade._read_member(src, '../../etc/passwd') is None
    assert upgrade._read_member(src, '') is None
    # A safe-but-missing member also returns None (not an exception).
    assert upgrade._read_member(src, 'arrangements/nope.json') is None


def test_safe_member_guard_rejects_non_string_names():
    # A crafted manifest can put a non-string where a member path is expected
    # (e.g. `lyrics: 1`); the guard must reject it rather than raise
    # AttributeError from .startswith() and 500 the route.
    assert upgrade._is_safe_archive_member(None) is False
    assert upgrade._is_safe_archive_member(1) is False
    assert upgrade._is_safe_archive_member(['a/b']) is False
    assert upgrade._is_safe_archive_member('arrangements/lead.json') is True


def test_manifest_fallback_is_bounded_like_the_host_path(tmp_path, monkeypatch):
    # The fallback must still respect the size cap the host reader enforces,
    # since it is what runs on untrusted uploads when the host is old.
    monkeypatch.setattr(upgrade, 'sloppak_mod', None)
    src = tmp_path / 'big.sloppak'
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('manifest.yaml', b'x' * (upgrade._MAX_MEMBER_BYTES + 1))
    with pytest.raises(FileNotFoundError):
        upgrade.load_archive_manifest(src)


# ── Route wiring ─────────────────────────────────────────────────────────

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
    import importlib
    import sys
    import types as _types

    fake_fastapi = _types.ModuleType('fastapi')
    fake_fastapi.WebSocket = object
    fake_fastapi.WebSocketDisconnect = Exception
    fake_responses = _types.ModuleType('fastapi.responses')
    fake_responses.FileResponse = object
    fake_responses.JSONResponse = lambda payload, status_code=200: {
        'payload': payload, 'status_code': status_code,
    }
    monkeypatch.setitem(sys.modules, 'fastapi', fake_fastapi)
    monkeypatch.setitem(sys.modules, 'fastapi.responses', fake_responses)
    sys.modules.pop('routes', None)
    return importlib.import_module('routes')


def test_compat_route_is_registered_and_returns_the_report(monkeypatch):
    import types as _types

    def load_sibling(name):
        # The route must return the *real* declared report, not a stub.
        if name == 'feedpakr_compat':
            return compat
        mod = _types.SimpleNamespace()
        if name == 'feedpakr_pipeline':
            mod.configure_sibling_loader = lambda loader: None
        return mod

    ctx = {
        'get_dlc_dir': lambda: None,
        'extract_meta': lambda path: {},
        'meta_db': _types.SimpleNamespace(put=lambda *a, **k: None),
        'log': _types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        ),
        'load_sibling': load_sibling,
    }

    routes = _load_routes(monkeypatch)
    app = _FakeApp()
    routes.setup(app, ctx)

    assert ('GET', '/api/plugins/feedpakr/compat') in app.handlers
    report = app.handlers[('GET', '/api/plugins/feedpakr/compat')]()
    assert report['core_baseline']['commit'] == compat.CORE_BASELINE_COMMIT
    assert set(report['features']) == set(compat.FEATURES)
    assert isinstance(report['ok'], bool)