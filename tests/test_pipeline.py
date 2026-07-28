"""Tests for feedpakr_pipeline.py. Needs the feedBack host core lib
(guitarpro, gp2rs, song, gp2midi) on sys.path — see conftest.py — and,
for the fixture-backed tests, the c:\\Users\\PC\\Downloads\\Alll sample
folder. Both self-skip cleanly when unavailable rather than false-failing
in an environment that only has this one repo checked out.
"""

from pathlib import Path

import pytest
import yaml

import feedpakr_pipeline as pipeline

HOST_AVAILABLE = (
    pipeline.guitarpro is not None
    and pipeline.gp2rs is not None
    and pipeline.gp2rs_gpx is not None
)

pytestmark = pytest.mark.skipif(
    not HOST_AVAILABLE, reason='feedBack host core lib not on sys.path'
)

FIXTURE_DIR = Path(r'c:\Users\PC\Downloads\Alll')
MONEY_GP5 = FIXTURE_DIR / 'Money (J).gp5'
MONEY_GP8 = FIXTURE_DIR / 'Money (J).gp'
fixture_available = pytest.mark.skipif(
    not MONEY_GP5.is_file(), reason='sample fixture not present on this machine'
)
gp8_fixture_available = pytest.mark.skipif(
    not MONEY_GP8.is_file(), reason='GP8 sample fixture not present on this machine'
)


def test_check_extension_accepts_gpx_and_gp():
    pipeline._check_extension('song.gpx')  # must not raise
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
        audio_mode='none',
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
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert any('authoring intermediate' in w for w in result['warnings'])
    assert 'manifest.yaml' in result['validation']  # empty stems fails minItems


# ── GPIF (.gp / .gpx, GP6/7/8) ────────────────────────────────────────────

@gp8_fixture_available
def test_parse_gp_gpif_detects_vocal_track():
    parsed = pipeline.parse_gp(str(MONEY_GP8))
    assert parsed['title'] == 'Money'
    vocal_tracks = [t for t in parsed['tracks'] if t['is_vocal']]
    assert len(vocal_tracks) == 1
    assert vocal_tracks[0]['auto_name'] == 'Vocals'


@gp8_fixture_available
def test_build_feedpak_gpif_vocal_track_becomes_lyrics_not_arrangement():
    """The GPIF vocal-track XML has root <vocals>, not <song> — routing it
    through song.parse_arrangement instead of the lyrics path was a real
    bug caught while implementing this (an empty-named ghost arrangement,
    no lyrics.json at all). Locks in the fix."""
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[0, 1, 3],  # Singer (vocal), Rhythm guitar, Bass
        arrangement_names={0: 'Vocals', 1: 'Rhythm', 3: 'Bass'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['arrangement_count'] == 2  # vocal track did NOT become an arrangement

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = zf.namelist()
        assert 'lyrics.json' in names
        lyrics = json.loads(zf.read('lyrics.json'))
        manifest = yaml.safe_load(zf.read('manifest.yaml'))

    assert len(lyrics) > 100  # Money has ~169 sung syllables
    assert lyrics[0]['w'] in ('Mo-', 'Money')  # real lyric, not empty
    assert manifest['lyrics_source'] == 'authored'
    assert manifest['arrangements'][0]['name'] != ''  # no ghost arrangement


@gp8_fixture_available
def test_gpif_capo_lookup_reads_capo_fret():
    """GPIF capo lives at Track/Staves/Staff/Properties/Property[@name=
    'CapoFret']/Fret — neither gp2rs nor gp2rs_gpx read it on their own
    (same class of gap as the GP3-5 capo fix). 'Money (J).gp' itself has
    no capo'd tracks, so this checks the extractor against a fixture that
    does, rather than only asserting an empty dict."""
    capo_path = FIXTURE_DIR / 'Three Days Grace-Animal I Have Become (Standard Tuning)-10-03-2024.gp'
    if not capo_path.is_file():
        pytest.skip('capo sample fixture not present on this machine')
    capo = pipeline._gpif_capo_lookup(str(capo_path))
    assert capo.get(6) == 2
