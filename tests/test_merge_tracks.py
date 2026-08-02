import feedpakr_pipeline as pipeline


def _wire(name, fret, template_name):
    return {
        'name': name,
        'tuning': [0, 0, 0, 0, 0, 0],
        'capo': 0,
        'centOffset': 0,
        'notes': [{'t': float(fret), 's': 0, 'f': fret}],
        'chords': [{'t': float(fret + 1), 'id': 0, 'notes': []}],
        'anchors': [{'time': float(fret), 'fret': fret, 'width': 4}],
        'handshapes': [{'chord_id': 0, 'start_time': float(fret + 1), 'end_time': float(fret + 2), 'arp': False}],
        'templates': [{'name': template_name, 'frets': [fret, -1, -1, -1, -1, -1]}],
    }


def test_merge_arrangement_wires_remaps_chord_references():
    merged = pipeline._merge_arrangement_wires(_wire('Rhythm', 1, 'A'), _wire('Rhythm', 3, 'C'))
    assert [chord['id'] for chord in merged['chords']] == [0, 1]
    assert [shape['chord_id'] for shape in merged['handshapes']] == [0, 1]
    assert [template['name'] for template in merged['templates']] == ['A', 'C']
    assert [note['f'] for note in merged['notes']] == [1, 3]


def test_combine_same_name_removes_second_file_case_insensitively():
    entries = [
        {'id': 'rhythm', 'name': 'Rhythm', 'file': 'arrangements/rhythm.json', 'tuning': [0] * 6, 'capo': 0},
        {'id': 'rhythm2', 'name': ' rhythm ', 'file': 'arrangements/rhythm2.json', 'tuning': [0] * 6, 'capo': 0},
    ]
    files = {'rhythm.json': _wire('Rhythm', 1, 'A'), 'rhythm2.json': _wire('Rhythm', 3, 'C')}
    warnings = []
    pipeline._combine_same_name_arrangements(entries, files, warnings)
    assert len(entries) == 1
    assert set(files) == {'rhythm.json'}
    assert len(files['rhythm.json']['notes']) == 2
    assert warnings == ['Combined 1 same-name track(s) into existing arrangements.']


def test_combine_same_name_keeps_incompatible_tunings_separate():
    entries = [
        {'id': 'rhythm', 'name': 'Rhythm', 'file': 'arrangements/rhythm.json', 'tuning': [0] * 6, 'capo': 0},
        {'id': 'rhythm2', 'name': 'Rhythm', 'file': 'arrangements/rhythm2.json', 'tuning': [0, 0, 0, 0, 0, -2], 'capo': 0},
    ]
    files = {'rhythm.json': _wire('Rhythm', 1, 'A'), 'rhythm2.json': _wire('Rhythm', 3, 'C')}
    warnings = []
    pipeline._combine_same_name_arrangements(entries, files, warnings)
    assert len(entries) == 2
    assert 'tuning, capo, or notation mode differs' in warnings[0]
