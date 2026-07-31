"""Regression tests against the 5 real '(J)' tabs in
c:\\Users\\PC\\Downloads\\Alll\\tabs (Black, Bring Me To Life, Money, Rehab,
So Far Away) — all GPIF (.gp) sources. These lock in the manual spot-checks
done during development: sections come out ordered and named sanely, chord
occurrences map consistently to their fret-pattern template, hand-shapes
are derived from them, and the (currently absent, in these particular
files) tone data is reported honestly rather than fabricated. Self-skips
when the sample folder or host lib isn't present, same convention as
test_pipeline.py.
"""
from pathlib import Path

import pytest

import feedpakr_pipeline as pipeline

HOST_AVAILABLE = (
    pipeline.guitarpro is not None
    and pipeline.gp2rs is not None
    and pipeline.gp2rs_gpx is not None
)

pytestmark = pytest.mark.skipif(
    not HOST_AVAILABLE, reason='feedBack host core lib not on sys.path'
)

TABS_DIR = Path(r'c:\Users\PC\Downloads\Alll\tabs')

# name -> expected (min_section_count, first_section_name)
CASES = {
    'Black (J).gp': (11, 'intro'),
    'Bring Me To Life (J).gp': (11, 'intro'),
    'Money (J).gp': (16, 'soundeffectsintro'),
    'Rehab (J).gp': (10, '1stverse'),
    'So Far Away (J).gp': (14, 'verse1'),
}


def _case_available(name):
    return pytest.mark.skipif(
        not (TABS_DIR / name).is_file(), reason=f'{name} not present on this machine',
    )


def _auto_selection(gp_path: str):
    parsed = pipeline.parse_gp(gp_path)
    track_indices = [t['index'] for t in parsed['tracks'] if t['auto_selected']]
    names = {t['index']: (t['auto_name'] or t['name']) for t in parsed['tracks'] if t['index'] in track_indices}
    return track_indices, names


@pytest.mark.parametrize('name', list(CASES))
def test_real_tab_sections_ordered_and_named(name):
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    min_count, first_name = CASES[name]
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path),
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        timeline = json.loads(zf.read('song_timeline.json'))

    sections = timeline['sections']
    assert len(sections) >= min_count
    assert sections[0]['name'] == first_name
    times = [s.get('time', s.get('start_time')) for s in sections]
    assert times == sorted(times)  # monotonically increasing, no reordering
    assert len(timeline['beats']) > 0


@pytest.mark.parametrize('name', list(CASES))
def test_real_tab_chords_map_consistently_to_templates(name):
    """Every chord occurrence sharing a template id must carry the same
    string/fret shape as that template — if this drifts, chord
    identification is broken, not just a display nicety (it drives the
    handshape/chord-highway rendering).

    Known, narrow exception (a real host gap, not a feedpakr bug): when a
    chord occurrence has two notes on the *same* string, gp2rs_gpx's
    chord-template builder keeps only the last one in that template's
    single-fret-per-string summary — `frets_t[n.string] = n.fret` in a
    plain loop over the chord's notes, discovered by this very test
    failing against 'Rehab (J).gp' drums and 'So Far Away (J).gp' rhythm.
    The per-occurrence note data itself (chord.notes, what's actually
    played) is NOT affected — only the auto-generated diagram/name preview
    for that one shape. feedpakr surfaces this as a build warning
    (see test_real_tab_warns_about_same_string_chord_collisions below);
    this test skips just those notes so it still catches genuine
    chord-id/template drift."""
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path),
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )

    import io
    import json
    import zipfile
    checked_any = False
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        for n in zf.namelist():
            if not n.startswith('arrangements/') or not n.endswith('.json'):
                continue
            arr = json.loads(zf.read(n))
            templates = arr.get('templates', [])
            for c in arr.get('chords', []):
                tid = c['id']
                assert 0 <= tid < len(templates), f'{name}/{n}: chord references unknown template {tid}'
                tpl_frets = templates[tid]['frets']
                strings_in_chord = [note['s'] for note in c['notes']]
                has_same_string_collision = len(set(strings_in_chord)) < len(strings_in_chord)
                if has_same_string_collision:
                    continue  # known host gap — can't be represented by a single-fret-per-string template
                for note in c['notes']:
                    s, f = note['s'], note['f']
                    assert tpl_frets[s] == f, (
                        f'{name}/{n}: chord id {tid} note string {s} fret {f} '
                        f"doesn't match its own template's frets {tpl_frets}"
                    )
                    checked_any = True
    assert checked_any, f'{name}: no chords found to check — selection may be wrong'


def test_black_authored_chord_names_reach_templates():
    """GP8 associates displayed chord names through Beat/Chord item refs;
    Diagram frets may be identical placeholders when ShowDiagram is false."""
    name = 'Black (J).gp'
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path), track_indices=track_indices,
        arrangement_names=names, audio_mode='none',
        report=lambda stage, pct: None,
    )

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        lead = json.loads(zf.read('arrangements/lead.json'))
        rhythm = json.loads(zf.read('arrangements/rhythm.json'))

    lead_names = {t['name'] for t in lead['templates'] if t.get('name')}
    rhythm_names = {t['name'] for t in rhythm['templates'] if t.get('name')}
    assert {'E', 'A', 'C'}.issubset(lead_names)
    assert {'E', 'A5', 'E7'}.issubset(rhythm_names)


@pytest.mark.parametrize('name', list(CASES))
def test_real_tab_warns_about_same_string_chord_collisions(name):
    """So Far Away's rhythm track hits the same-string chord-template gap
    described above, and feedpakr must surface it rather than stay silent."""
    # Rehab's collisions were all on its drum track; proper GPIF drum-tab
    # routing no longer builds guitar-style chord templates for that track.
    KNOWN_AFFECTED = {'So Far Away (J).gp'}
    if name not in KNOWN_AFFECTED:
        pytest.skip('not a known-affected fixture')
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path),
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert any('same string' in w for w in result['warnings']), result['warnings']


@pytest.mark.parametrize('name', list(CASES))
def test_real_tab_handshapes_derived_when_chords_present(name):
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path),
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['features']['handshapes'] is True
    # Every fixture contains at least one GPIF percussion track. It must be
    # routed to drum-tab instead of contributing guitar-style handshapes.
    assert result['features']['drum_arrangements'] >= 1


@pytest.mark.parametrize('name', list(CASES))
def test_real_tab_tones_present_via_sound_automation(name):
    """All 5 tabs carry NO <Bank>-per-beat tone data (the older,
    guitar/bass-oriented mechanism gp2rs_gpx._collect_tone_events reads) —
    confirmed by direct inspection, and the original state of this test
    before the fix below. But every GPIF track also declares its MIDI/RSE
    instrument via a *different*, track-level `<Automations Type="Sound">`
    mechanism (feedpak spec §6.9's `tones`), which the host never read at
    all (not even for guitar tracks, and explicitly skipped for keys
    tracks). feedpakr_tones.extract_gpif_sound_changes closes that gap, so
    every arrangement in every one of these songs now carries real tones
    data (at minimum, its base instrument declaration)."""
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path),
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['features']['tones'] is True


def test_bring_me_to_life_keys_track_captures_piano_to_strings_switch():
    """Regression test for the exact bug report that led to this feature:
    'Bring Me To Life (J).gp' track 5 ("Piano (SS)", auto-named "Combo")
    declares two Sounds (Bright Acoustic Piano, Ensemble/Violin) and
    genuinely switches between them via Automations — confirmed by direct
    GPIF inspection: bar 0 piano, bar 13 (t=32.5s) strings, bar 85
    (t=211.25s) back to piano. Locks in the exact names and timestamps."""
    name = 'Bring Me To Life (J).gp'
    gp_path = TABS_DIR / name
    if not gp_path.is_file():
        pytest.skip(f'{name} not present on this machine')
    track_indices, names = _auto_selection(str(gp_path))

    result = pipeline.build_feedpak(
        str(gp_path),
        track_indices=track_indices,
        arrangement_names=names,
        audio_mode='none',
        report=lambda stage, pct: None,
    )

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        combo = json.loads(zf.read('arrangements/combo.json'))

    tones = combo['tones']
    assert tones['base'] == 'Bright Acoustic Piano'
    changes = tones['changes']
    assert [c['name'] for c in changes] == [
        'Bright Acoustic Piano', 'Ensemble', 'Bright Acoustic Piano',
    ]
    assert changes[0]['t'] == pytest.approx(0.0, abs=0.01)
    assert changes[1]['t'] == pytest.approx(32.5, abs=0.01)
    assert changes[2]['t'] == pytest.approx(211.25, abs=0.01)
