"""Unit tests for feedpakr_keys.py — pure functions, no GP file needed
for key_name_for(); the extract_* functions get a light real-fixture
smoke test in test_pipeline.py."""

import feedpakr_keys as keys_mod


def test_key_name_for_c_major():
    assert keys_mod.key_name_for(0, False) == ('C', 'major')


def test_key_name_for_a_minor():
    assert keys_mod.key_name_for(0, True) == ('Am', 'natural_minor')


def test_key_name_for_sharps():
    assert keys_mod.key_name_for(1, False) == ('G', 'major')
    assert keys_mod.key_name_for(3, False) == ('A', 'major')
    assert keys_mod.key_name_for(6, False) == ('F#', 'major')


def test_key_name_for_flats():
    assert keys_mod.key_name_for(-1, False) == ('F', 'major')
    assert keys_mod.key_name_for(-3, False) == ('Eb', 'major')


def test_key_name_for_relative_minor_shares_accidentals():
    # E minor is the relative minor of G major — both have 1 sharp.
    assert keys_mod.key_name_for(1, True) == ('Em', 'natural_minor')


def test_key_name_for_unknown_count_falls_back_to_c():
    assert keys_mod.key_name_for(99, False) == ('C', 'major')


def test_extract_gpif_keys_reads_masterbar_key(tmp_path):
    xml = '''<?xml version="1.0"?>
<GPIF>
  <MasterBars>
    <MasterBar>
      <Key><AccidentalCount>0</AccidentalCount><Mode>Major</Mode></Key>
    </MasterBar>
    <MasterBar>
      <Key><AccidentalCount>2</AccidentalCount><Mode>Minor</Mode></Key>
    </MasterBar>
  </MasterBars>
</GPIF>'''
    from xml.etree import ElementTree as ET
    root = ET.fromstring(xml)
    beats = [{'time': 0.0, 'measure': 1}, {'time': 10.0, 'measure': 2}]
    result = keys_mod.extract_gpif_keys(root, beats)
    assert result == {
        'version': 1,
        'events': [
            {'t': 0.0, 'key': 'C', 'scale': 'major'},
            {'t': 10.0, 'key': 'Bm', 'scale': 'natural_minor'},
        ],
    }


def test_extract_gpif_keys_dedupes_unchanged_key(tmp_path):
    xml = '''<?xml version="1.0"?>
<GPIF>
  <MasterBars>
    <MasterBar><Key><AccidentalCount>0</AccidentalCount><Mode>Major</Mode></Key></MasterBar>
    <MasterBar><Key><AccidentalCount>0</AccidentalCount><Mode>Major</Mode></Key></MasterBar>
  </MasterBars>
</GPIF>'''
    from xml.etree import ElementTree as ET
    root = ET.fromstring(xml)
    beats = [{'time': 0.0, 'measure': 1}, {'time': 10.0, 'measure': 2}]
    result = keys_mod.extract_gpif_keys(root, beats)
    assert len(result['events']) == 1


def test_extract_gpif_keys_no_beats_returns_none():
    from xml.etree import ElementTree as ET
    root = ET.fromstring('<GPIF><MasterBars/></GPIF>')
    assert keys_mod.extract_gpif_keys(root, []) is None
