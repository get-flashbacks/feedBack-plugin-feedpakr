"""Tests for feedpakr_pipeline.py. Needs the feedBack host core lib
(guitarpro, gp2rs, song, gp2midi) on sys.path — see conftest.py — and,
for the fixture-backed tests, the c:\\Users\\PC\\Downloads\\Alll sample
folder. Both self-skip cleanly when unavailable rather than false-failing
in an environment that only has this one repo checked out.
"""

from pathlib import Path

import pytest

import feedpakr_pipeline as pipeline

HOST_AVAILABLE = pipeline.guitarpro is not None and pipeline.gp2rs is not None

pytestmark = pytest.mark.skipif(
    not HOST_AVAILABLE, reason='feedBack host core lib not on sys.path'
)

FIXTURE_DIR = Path(r'c:\Users\PC\Downloads\Alll')
MONEY_GP5 = FIXTURE_DIR / 'Money (J).gp5'
fixture_available = pytest.mark.skipif(
    not MONEY_GP5.is_file(), reason='sample fixture not present on this machine'
)


def test_check_extension_rejects_gpx_and_gp():
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension('song.gpx')
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension('song.gp')


def test_check_extension_rejects_unknown():
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension('song.mid')


def test_check_extension_accepts_gp345():
    for ext in ('song.gp3', 'song.gp4', 'song.gp5'):
        pipeline._check_extension(ext)  # must not raise


def test_capo_for_track_reads_offset():
    song = pipeline.guitarpro.Song()
    song.tracks[0].offset = 3
    assert pipeline._capo_for_track(song, 0) == 3


def test_capo_for_track_defaults_to_zero_when_none():
    song = pipeline.guitarpro.Song()
    song.tracks[0].offset = None
    assert pipeline._capo_for_track(song, 0) == 0


def test_capo_for_track_out_of_range_returns_zero():
    song = pipeline.guitarpro.Song()
    assert pipeline._capo_for_track(song, 99) == 0


@fixture_available
def test_parse_gp_reads_money_metadata():
    parsed = pipeline.parse_gp(str(MONEY_GP5))
    assert parsed['title'] == 'Money'
    assert parsed['artist'] == 'Pink Floyd'
    assert len(parsed['tracks']) > 0


@fixture_available
def test_build_feedpak_extracts_all_16_sections():
    """Regression test locking in the audit's flagship fix: 'Money (J).gp'
    was documented as losing all 16 of its section markers through the
    legacy sloppak pipeline. The GP5 companion file must not regress."""
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1, 3],
        arrangement_names={1: 'Rhythm', 3: 'Bass'},
        want_audio=False,
        report=lambda stage, pct: None,
    )
    assert result['arrangement_count'] == 2
    assert result['duration'] > 300  # Money runs ~6:26

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        timeline = json.loads(zf.read('song_timeline.json'))
    assert len(timeline['sections']) == 16
    assert timeline['sections'][0]['name'] == 'soundeffectsintro'
    assert len(timeline['beats']) > 0


@fixture_available
def test_build_feedpak_without_audio_warns_authoring_intermediate():
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        want_audio=False,
        report=lambda stage, pct: None,
    )
    assert any('authoring intermediate' in w for w in result['warnings'])
    assert 'manifest.yaml' in result['validation']  # empty stems fails minItems
