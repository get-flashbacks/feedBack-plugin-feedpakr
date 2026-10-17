"""Required conversion regressions against portable, generated fixtures.

These are the release-gating import tests. They run in CI (where the host
core lib is checked out) and use fixtures authored in-repo by
``tests/portable_fixtures.py`` — never the developer's personal sample
library. They cover what the issue (#81) calls out as essential:

* conversion section markers come out ordered and named sanely,
* chord occurrences map consistently to their fret-pattern templates,
* the two import paths (GPIF for GP7/8, GP3-5 via pyguitarpro) behave as
  their formats require and are distinguishable.

Because the fixtures are always available, these tests must **not** be
skipped for lack of fixtures. When the host lib is missing, CI sets
``FEEDPAKR_REQUIRE=host`` and ``tests/conftest.py`` aborts the run before
collection instead of letting this module self-skip (see tests/SKIPS.md).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
import yaml

import feedpakr_pipeline as pipeline
import portable_fixtures as fixtures

HOST_AVAILABLE = (
    pipeline.guitarpro is not None
    and pipeline.gp2rs is not None
    and pipeline.gp2rs_gpx is not None
)

pytestmark = pytest.mark.skipif(
    not HOST_AVAILABLE, reason='feedBack host core lib not on sys.path'
)


# ── Helpers ───────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def gpif_path(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp('gpif') / 'portable.gp'
    return str(fixtures.write_gpif_fixture(path))


@pytest.fixture(scope='module')
def gp345_path(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp('gp345') / 'portable.gp4'
    return str(fixtures.write_gp345_fixture(path))


def _build(gp_path: str, track_indices, names) -> dict:
    return pipeline.build_feedpak(
        gp_path,
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )


def _read_json(zf: zipfile.ZipFile, name: str) -> dict:
    return json.loads(zf.read(name))


def _assert_chords_match_templates(arr: dict, label: str) -> int:
    """Return the number of chord notes checked, asserting each note agrees
    with its template's fret-pattern (the chord-highway/handshape contract)."""
    templates = arr.get('templates', [])
    checked = 0
    for chord in arr.get('chords', []):
        tid = chord['id']
        assert 0 <= tid < len(templates), f'{label}: unknown template {tid}'
        frets = templates[tid]['frets']
        for note in chord['notes']:
            assert frets[note['s']] == note['f'], (
                f'{label}: chord {tid} note string {note["s"]} fret {note["f"]} '
                f'does not match template frets {frets}'
            )
            checked += 1
    return checked


# ── GPIF (GP7/8) ──────────────────────────────────────────────────────────

def test_gpif_sections_ordered_and_named(gpif_path):
    result = _build(gpif_path, track_indices=[0], names={0: 'Lead'})
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        timeline = _read_json(zf, 'song_timeline.json')
    sections = timeline['sections']
    assert [s['name'] for s in sections] == ['intro', 'verse', 'chorus']
    times = [s['time'] for s in sections]
    assert times == sorted(times)
    assert times == [0.0, 2.0, 4.0]
    assert len(timeline['beats']) > 0


def test_gpif_chord_occurrences_match_their_templates(gpif_path):
    result = _build(gpif_path, track_indices=[0], names={0: 'Lead'})
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        arr = _read_json(zf, 'arrangements/lead.json')
    assert _assert_chords_match_templates(arr, 'gpif') >= 1
    shapes = tuple(
        tuple(sorted((n['s'], n['f']) for n in c['notes']))
        for c in arr['chords']
    )
    assert shapes == tuple(
        tuple(sorted(shape)) for shape in fixtures.GPIF_EXPECTED_CHORD_SHAPES
    )


def test_gpif_tuning_reads_absolute_pitches_as_offsets(gpif_path):
    """GPIF stores absolute MIDI pitches; feedpakr emits relative semitone
    offsets from standard tuning. The fixture drops the low string to D, so
    only the first offset moves — a GPIF-format-specific conversion."""
    result = _build(gpif_path, track_indices=[0], names={0: 'Lead'})
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        arr = _read_json(zf, 'arrangements/lead.json')
    assert arr['tuning'] == [-2, 0, 0, 0, 0, 0]


# ── GP3-5 (GP4) ───────────────────────────────────────────────────────────

def test_gp345_sections_ordered_and_named(gp345_path):
    result = _build(gp345_path, track_indices=[0, 1], names={0: 'Lead', 1: 'Drums'})
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        timeline = _read_json(zf, 'song_timeline.json')
    assert [s['name'] for s in timeline['sections']] == ['intro', 'verse', 'chorus']
    assert [s['time'] for s in timeline['sections']] == [0.0, 2.0, 4.0]


def test_gp345_chord_occurrences_match_their_templates(gp345_path):
    result = _build(gp345_path, track_indices=[0, 1], names={0: 'Lead', 1: 'Drums'})
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        arr = _read_json(zf, 'arrangements/lead.json')
    assert _assert_chords_match_templates(arr, 'gp345') >= 1
    assert len(arr['chords']) == len(fixtures.GP345_LEAD_CHORDS)
    # Pin the actual fret shapes (not just template self-consistency) so a
    # systematic GP3-5 string/fret misread can't pass while staying internally
    # consistent. The authored shapes are the oracle.
    shapes = tuple(
        tuple(sorted((n['s'], n['f']) for n in c['notes']))
        for c in arr['chords']
    )
    assert shapes == fixtures.GP345_EXPECTED_CHORD_SHAPES


def test_gp345_repeated_chord_shape_reuses_one_template(gp345_path):
    """The third chord repeats the first shape verbatim; a regression that
    re-minted a template per occurrence would show up here."""
    result = _build(gp345_path, track_indices=[0, 1], names={0: 'Lead', 1: 'Drums'})
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        arr = _read_json(zf, 'arrangements/lead.json')
    chords = arr['chords']
    assert chords[0]['id'] == chords[2]['id']
    assert chords[0]['id'] != chords[1]['id']


def test_gp345_drums_become_drum_tab(gp345_path):
    result = _build(gp345_path, track_indices=[0, 1], names={0: 'Lead', 1: 'Drums'})
    assert result['features']['drum_arrangements'] == 1
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        drum_tab = _read_json(zf, 'drum_tab_drums.json')
    drum_arrs = [a for a in manifest['arrangements'] if a.get('type') == 'drums']
    assert len(drum_arrs) == 1
    # Pin the MIDI→piece mapping, the hit order/times, and the display names
    # (not just the set of pieces) so a kick/snare swap or a dropped/duplicated
    # hit can't pass.
    hits = [(hit['t'], hit['p']) for hit in drum_tab['hits']]
    assert hits == [(0.0, 'kick'), (2.0, 'snare'), (4.0, 'kick')]
    kit = {piece['id']: piece['name'] for piece in drum_tab['kit']}
    assert kit == fixtures.GP345_DRUM_PIECE_NAMES


# ── Format dispatch ───────────────────────────────────────────────────────

def test_format_detection_differs_between_gpif_and_gp345(gpif_path, gp345_path):
    """`parse_gp` routes by file extension (``.gp``/`.gp*/`), not by sniffing
    content, so this only asserts the two extensions take different parsers.
    What each path actually *produces* is pinned by the format-specific tests
    above — this is the routing smoke test, not the behavioral oracle."""
    assert pipeline.parse_gp(gpif_path)['format'] == 'gpif'
    assert pipeline.parse_gp(gp345_path)['format'] == 'gp345'
