"""Unit tests for feedpakr_handshapes.py — pure dict logic, no GP file needed."""

import feedpakr_handshapes as hs_mod


def _wire(chords, templates=None):
    return {'chords': chords, 'templates': templates or []}


def test_no_chords_returns_empty():
    assert hs_mod.derive_handshapes(_wire([])) == []


def test_single_chord_uses_own_sustain():
    wire = _wire(
        chords=[{'t': 1.0, 'id': 0, 'notes': [{'sus': 2.0}, {'sus': 1.0}]}],
        templates=[{'name': 'Am', 'arp': False}],
    )
    shapes = hs_mod.derive_handshapes(wire)
    assert shapes == [{'chord_id': 0, 'start_time': 1.0, 'end_time': 3.0, 'arp': False}]


def test_single_chord_no_sustain_falls_back_to_default():
    wire = _wire(chords=[{'t': 1.0, 'id': 0, 'notes': [{'sus': 0}]}])
    shapes = hs_mod.derive_handshapes(wire)
    assert shapes[0]['end_time'] == 1.5  # 1.0 + _DEFAULT_SUSTAIN (0.5)


def test_consecutive_same_chord_merges_into_one_span():
    wire = _wire(chords=[
        {'t': 0.0, 'id': 0, 'notes': [{'sus': 0.2}]},
        {'t': 0.5, 'id': 0, 'notes': [{'sus': 0.2}]},
        {'t': 1.0, 'id': 0, 'notes': [{'sus': 0.2}]},
    ])
    shapes = hs_mod.derive_handshapes(wire)
    assert len(shapes) == 1
    assert shapes[0]['start_time'] == 0.0
    assert shapes[0]['end_time'] == 1.2


def test_differing_chord_ids_produce_separate_spans():
    wire = _wire(chords=[
        {'t': 0.0, 'id': 0, 'notes': [{'sus': 0.5}]},
        {'t': 1.0, 'id': 1, 'notes': [{'sus': 0.5}]},
    ])
    shapes = hs_mod.derive_handshapes(wire)
    assert [s['chord_id'] for s in shapes] == [0, 1]


def test_span_capped_at_next_differing_chord():
    # Own sustain would run to t=5, but the next (different) chord starts
    # at t=2 — the span must not overlap it.
    wire = _wire(chords=[
        {'t': 0.0, 'id': 0, 'notes': [{'sus': 5.0}]},
        {'t': 2.0, 'id': 1, 'notes': [{'sus': 0.5}]},
    ])
    shapes = hs_mod.derive_handshapes(wire)
    assert shapes[0]['end_time'] == 2.0


def test_arp_flag_taken_from_matching_template():
    wire = _wire(
        chords=[{'t': 0.0, 'id': 0, 'notes': [{'sus': 0.5}]}],
        templates=[{'name': 'Am', 'arp': True}],
    )
    shapes = hs_mod.derive_handshapes(wire)
    assert shapes[0]['arp'] is True


def test_out_of_range_chord_id_defaults_arp_false():
    wire = _wire(chords=[{'t': 0.0, 'id': 5, 'notes': []}], templates=[])
    shapes = hs_mod.derive_handshapes(wire)
    assert shapes[0]['arp'] is False
