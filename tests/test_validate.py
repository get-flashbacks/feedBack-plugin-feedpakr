"""Unit tests for feedpakr_validate.py against the vendored feedpak-spec
schemas. If jsonschema isn't installed, these tests self-skip rather than
false-fail — the module itself degrades the same way at runtime."""

import json
import zipfile

import pytest
import yaml

import feedpakr_validate as validate

pytestmark = pytest.mark.skipif(
    not validate.available(), reason='jsonschema not installed'
)


def _valid_manifest(**overrides):
    manifest = {
        'feedpak_version': '1.19.0',
        'title': 'Test Song',
        'artist': 'Test Artist',
        'duration': 120.0,
        'arrangements': [
            {'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json',
             'tuning': [0, 0, 0, 0, 0, 0], 'capo': 0},
        ],
        'stems': [{'id': 'full', 'file': 'stems/full.ogg', 'default': True}],
    }
    manifest.update(overrides)
    return manifest


def test_valid_manifest_has_no_errors():
    assert validate.validate_manifest(_valid_manifest()) == []


def test_manifest_missing_required_field_fails():
    manifest = _valid_manifest()
    del manifest['duration']
    errors = validate.validate_manifest(manifest)
    assert errors
    assert any('duration' in e for e in errors)


def test_manifest_empty_stems_fails_minitems():
    manifest = _valid_manifest(stems=[])
    errors = validate.validate_manifest(manifest)
    assert errors


def test_manifest_bad_relpath_fails():
    manifest = _valid_manifest()
    manifest['cover'] = '../evil.png'
    errors = validate.validate_manifest(manifest)
    assert errors


def test_valid_arrangement_has_no_errors():
    arrangement = {
        'name': 'Lead',
        'tuning': [0, 0, 0, 0, 0, 0],
        'capo': 0,
        'centOffset': 0.0,
        'notes': [{'t': 0.0, 's': 0, 'f': 3}],
        'chords': [],
        'anchors': [],
        'handshapes': [],
        'templates': [],
    }
    assert validate.validate_arrangement(arrangement) == []


def test_valid_song_timeline_has_no_errors():
    timeline = {
        'version': 1,
        'beats': [{'time': 0.0, 'measure': 1}],
        'sections': [{'name': 'Verse', 'number': 1, 'time': 0.0}],
    }
    assert validate.validate_song_timeline(timeline) == []


def test_validate_pack_aggregates_only_failing_parts():
    manifest = _valid_manifest()
    arrangement_files = {
        'lead.json': {
            'name': 'Lead', 'tuning': [0] * 6, 'capo': 0, 'centOffset': 0.0,
            'notes': [{'t': 0.0, 's': 0, 'f': 3}],
            'chords': [], 'anchors': [], 'handshapes': [], 'templates': [],
        },
    }
    report = validate.validate_pack(manifest=manifest, arrangement_files=arrangement_files)
    assert report == {}


def test_validate_pack_reports_broken_arrangement():
    manifest = _valid_manifest()
    arrangement_files = {'lead.json': {'notes': [{'s': 0, 'f': 3}]}}  # note missing required 't'
    report = validate.validate_pack(manifest=manifest, arrangement_files=arrangement_files)
    assert 'arrangements/lead.json' in report


def test_validate_feedpak_file_reads_referenced_parts(tmp_path):
    path = tmp_path / 'valid.feedpak'
    arrangement = {
        'name': 'Lead', 'tuning': [0] * 6, 'capo': 0, 'centOffset': 0.0,
        'notes': [{'t': 0.0, 's': 0, 'f': 3}], 'chords': [], 'anchors': [],
        'handshapes': [], 'templates': [],
    }
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(_valid_manifest()))
        zf.writestr('arrangements/lead.json', json.dumps(arrangement))
    assert validate.validate_feedpak_file(path) == {}


def test_validate_feedpak_file_reports_missing_reference(tmp_path):
    path = tmp_path / 'broken.feedpak'
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(_valid_manifest()))
    report = validate.validate_feedpak_file(path)
    assert report['arrangements/lead.json'] == ['<root>: referenced file is missing']


def test_validate_feedpak_file_reports_missing_manifest(tmp_path):
    path = tmp_path / 'missing-manifest.feedpak'
    with zipfile.ZipFile(path, 'w'):
        pass
    assert validate.validate_feedpak_file(path) == {
        'manifest.yaml': ['<root>: manifest file is missing'],
    }


def test_validate_feedpak_file_reports_non_object_manifest(tmp_path):
    path = tmp_path / 'list-manifest.feedpak'
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('manifest.yaml', '- not\n- an object\n')
    assert validate.validate_feedpak_file(path) == {
        'manifest.yaml': ['<root>: manifest must be an object'],
    }


def test_validate_feedpak_file_reports_unparsable_json_sidecar(tmp_path):
    path = tmp_path / 'malformed-sidecar.feedpak'
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(_valid_manifest()))
        zf.writestr('arrangements/lead.json', '{not json')
    report = validate.validate_feedpak_file(path)
    assert report['arrangements/lead.json'][0].startswith('<root>: could not parse JSON:')


def test_validate_feedpak_file_raises_for_corrupt_archive(tmp_path):
    path = tmp_path / 'corrupt.feedpak'
    path.write_bytes(b'not a zip')
    with pytest.raises(zipfile.BadZipFile):
        validate.validate_feedpak_file(path)
