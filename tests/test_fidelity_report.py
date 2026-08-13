import io
import json
import zipfile
import xml.etree.ElementTree as ET

import pytest
import yaml

import compare_gp_to_feedpak as fidelity

# Only the GPIF test below reaches into the host's gp2rs_gpx module; the other
# two tests in this file are self-contained. Without the sibling feedBack
# checkout, pipeline.gp2rs_gpx is None and monkeypatching an attribute on it
# raises AttributeError instead of skipping — which contradicted conftest's
# "tests that need it will self-skip" contract and made a plugin-only checkout
# look broken. Same idiom as test_pipeline.py's HOST_AVAILABLE gate, applied
# per-test rather than as a module-wide pytestmark so the other two keep
# running standalone.
host_required = pytest.mark.skipif(
    fidelity.pipeline.gp2rs_gpx is None,
    reason='feedBack host core lib not on sys.path',
)


def _pack_bytes():
    manifest = {
        'title': 'Song', 'artist': 'Artist', 'album': 'Album',
        'arrangements': [
            {'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json', 'capo': 2},
            {'id': 'drums', 'name': 'Drums', 'type': 'drums', 'drum_tab': 'drum_tab_drums.json'},
        ],
    }
    arrangement = {
        'notes': [{'t': 0, 's': 0, 'f': 3, 'pkd': 0}],
        'chords': [{'t': 1, 'id': 0, 'notes': [
            {'s': 0, 'f': 3, 'pkd': 1}, {'s': 1, 'f': 2, 'pkd': 1},
        ]}],
        'tones': {'base': 'Clean', 'changes': []},
    }
    timeline = {'sections': [{'name': 'intro', 'time': 0}], 'beats': [{'time': 0}]}
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(manifest))
        zf.writestr('arrangements/lead.json', json.dumps(arrangement))
        zf.writestr('song_timeline.json', json.dumps(timeline))
        zf.writestr('drum_tab_drums.json', json.dumps({'hits': [{'t': 0, 'midi': 36}]}))
        zf.writestr('keys.json', '{}')
        zf.writestr('lyrics.json', '[]')
        zf.writestr('stems/full.ogg', b'OggS')
    return out.getvalue()


def test_inspect_feedpak_counts_playable_notes_and_features():
    got = fidelity.inspect_feedpak(_pack_bytes())
    assert got['arrangements'] == 2
    assert got['notes'] == 3
    assert got['chords'] == 1
    assert got['strumming'] == 3
    assert got['capo'] is True
    assert got['sections'] is True
    assert got['beats'] is True
    assert got['drums'] == 1
    assert got['tones'] is True
    assert got['embedded_audio'] is True


def test_compare_distinguishes_missing_absent_and_skipped_audio():
    source = {'title': True, 'lyrics': False, 'strumming': 2, 'embedded_audio': True}
    output = {'title': True, 'lyrics': False, 'strumming': 0, 'embedded_audio': False}
    rows = {row['field']: row for row in fidelity.compare(source, output, audio_tested=False)}
    assert rows['title']['status'] == 'preserved'
    assert rows['lyrics']['status'] == 'not-present'
    assert rows['strumming']['status'] == 'missing'
    assert rows['embedded_audio']['status'] == 'skipped'


@host_required
def test_gpif_source_counts_played_chords_and_ignores_zero_capo(monkeypatch):
    root = ET.fromstring('''
        <GPIF>
          <Score><Title>Song</Title><Artist>Artist</Artist></Score>
          <Properties><Property name="CapoFret"><Fret>0</Fret></Property></Properties>
          <Beats>
            <Beat id="1"><Notes>10 11 12</Notes></Beat>
            <Beat id="2"><Notes>13</Notes></Beat>
          </Beats>
          <Notes><Note id="10"/><Note id="11"/><Note id="12"/><Note id="13"/></Notes>
        </GPIF>
    ''')
    monkeypatch.setattr(fidelity.pipeline.gp2rs_gpx, '_load_gpif', lambda _path: root)
    parsed = {'tracks': [{'index': 0, 'is_vocal': False}]}
    got = fidelity._gpif_source('fixture.gp', parsed, {0})
    assert got['chords'] == 1
    assert got['capo'] is False
