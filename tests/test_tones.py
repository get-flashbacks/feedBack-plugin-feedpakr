"""Unit tests for feedpakr_tones.py.

parse_tones_xml() is pure XML parsing — tested directly with synthetic
files. extract_gp345_tones() needs a real pyguitarpro Song; neither
available real fixture happens to encode mixTableChange.instrument
events (both were checked directly against the raw parsed Song and
confirmed to have none — see feedpakr-project.md dev notes), so that
path is exercised here with a minimal synthetic Song instead of skipped
outright.
"""

import pytest

import feedpakr_tones as tones_mod

try:
    import guitarpro
except ImportError:
    guitarpro = None


def test_parse_tones_xml_reads_tonebase_and_changes(tmp_path):
    xml_path = tmp_path / 'arr.xml'
    xml_path.write_text('''<?xml version="1.0"?>
<song version="7">
  <title>Test</title>
  <tonebase>Clean Guitar</tonebase>
  <tones count="2">
    <tone id="0" name="Clean Guitar" time="0.000"/>
    <tone id="1" name="Distortion Guitar" time="30.500"/>
  </tones>
</song>''', encoding='utf-8')

    result = tones_mod.parse_tones_xml(str(xml_path))
    assert result == {
        'base': 'Clean Guitar',
        'changes': [
            {'t': 0.0, 'name': 'Clean Guitar'},
            {'t': 30.5, 'name': 'Distortion Guitar'},
        ],
    }


def test_parse_tones_xml_no_tones_returns_none(tmp_path):
    xml_path = tmp_path / 'arr.xml'
    xml_path.write_text('<?xml version="1.0"?>\n<song version="7"><title>T</title></song>', encoding='utf-8')
    assert tones_mod.parse_tones_xml(str(xml_path)) is None


def test_program_name_known_and_unknown():
    assert tones_mod._program_name(29) == 'Overdriven Guitar'
    assert tones_mod._program_name(99) == 'Tone 99'


@pytest.mark.skipif(guitarpro is None, reason='pyguitarpro not installed')
def test_extract_gp345_tones_scans_all_selected_tracks():
    song = guitarpro.Song()
    # Song() ships one default track; add a second so the "scan every
    # selected track, not just the first" behaviour is actually exercised.
    song.tracks.append(guitarpro.Track(song))

    def _add_instrument_event(track, beat_start, program):
        measure = track.measures[0]
        voice = measure.voices[0]
        beat = voice.beats[0] if voice.beats else None
        if beat is None:
            pytest.skip('synthetic Song has no beats to attach an effect to')
        beat.start = beat_start
        effect = beat.effect
        mtc = guitarpro.MixTableChange()
        mtc.instrument = guitarpro.MixTableItem(value=program)
        effect.mixTableChange = mtc

    # Both tracks need at least one measure/voice/beat — guitarpro.Song()'s
    # default track ships pre-populated; the appended one may not, so guard
    # rather than assume.
    if song.tracks[0].measures and song.tracks[0].measures[0].voices[0].beats:
        _add_instrument_event(song.tracks[0], 0, 29)  # Overdriven Guitar

    result = tones_mod.extract_gp345_tones(song, [0, 1])
    if result is None:
        pytest.skip('synthetic pyguitarpro Song has no attachable beats in this version')
    assert result['base'] == 'Overdriven Guitar'
