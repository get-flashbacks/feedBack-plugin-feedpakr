"""Key/scale extraction (keys.json, feedpak spec §7.7) for both source
families. Neither the sloppak format nor gp2rs/gp2rs_gpx had anywhere to
put this — it's a genuinely new capability feedpak enables, not a gap in
an existing converter.

Both GP3/4/5 (pyguitarpro `MeasureHeader.keySignature`, one per measure)
and GPIF (`MasterBar/Key/AccidentalCount`+`Mode`, one per bar) expose a
per-measure key signature as (accidental_count, is_minor) — a signed
count of sharps(+)/flats(-) plus a major/minor flag, not a note name.
_key_name_for() spells the actual tonic from that pair via the standard
circle-of-fifths table, since the raw pyguitarpro KeySignature enum names
(e.g. "FMajorSharp") are systematic labels, not real key names.
"""

from __future__ import annotations

# accidental_count -> (major tonic, minor tonic), standard circle-of-fifths
# spelling. Index 0 = no sharps/flats (C major / A minor).
_KEY_TONICS = {
    -8: ('Fb', 'Db'), -7: ('Cb', 'Ab'), -6: ('Gb', 'Eb'), -5: ('Db', 'Bb'),
    -4: ('Ab', 'F'), -3: ('Eb', 'C'), -2: ('Bb', 'G'), -1: ('F', 'D'),
    0: ('C', 'A'), 1: ('G', 'E'), 2: ('D', 'B'), 3: ('A', 'F#'),
    4: ('E', 'C#'), 5: ('B', 'G#'), 6: ('F#', 'D#'), 7: ('C#', 'A#'),
    8: ('G#', 'E#'),
}


def key_name_for(accidental_count: int, is_minor: bool) -> tuple[str, str]:
    """Returns (key, scale) in the spec's convention: key is a tonic name
    with a trailing 'm' for minor ("Em", "G"), scale is "major" or
    "natural_minor"."""
    major, minor = _KEY_TONICS.get(accidental_count, ('C', 'A'))
    if is_minor:
        return f'{minor}m', 'natural_minor'
    return major, 'major'


def _measure_times(beats: list[dict]) -> dict[int, float]:
    return {b['measure']: b['time'] for b in beats if b.get('measure', -1) >= 0}


def extract_gp345_keys(gp_song, beats: list[dict]) -> dict | None:
    """Walk MeasureHeader.keySignature per measure, emitting one event per
    actual key change (song.key is a single KeySignature enum whose .value
    is (accidental_count, is_minor) — see the module docstring)."""
    measure_times = _measure_times(beats)
    if not measure_times:
        return None

    events: list[dict] = []
    last_sig: tuple[int, int] | None = None
    for i, header in enumerate(gp_song.measureHeaders):
        measure_num = i + 1  # ebeats/measure numbering is 1-based
        if measure_num not in measure_times:
            continue
        sig = getattr(header, 'keySignature', None)
        if sig is None:
            continue
        accidentals, is_minor = sig.value
        if (accidentals, is_minor) == last_sig:
            continue
        last_sig = (accidentals, is_minor)
        key, scale = key_name_for(accidentals, bool(is_minor))
        events.append({'t': measure_times[measure_num], 'key': key, 'scale': scale})

    return {'version': 1, 'events': events} if events else None


def extract_gpif_keys(gpif_root, beats: list[dict]) -> dict | None:
    """Same idea for GPIF: MasterBar/Key/AccidentalCount + Mode, one
    MasterBar per bar (1-based, matching ebeats' measure numbering)."""
    measure_times = _measure_times(beats)
    if not measure_times:
        return None

    masterbars = gpif_root.find('MasterBars')
    if masterbars is None:
        return None

    events: list[dict] = []
    last_sig: tuple[int, bool] | None = None
    for i, mb in enumerate(masterbars):
        measure_num = i + 1
        if measure_num not in measure_times:
            continue
        key_el = mb.find('Key')
        if key_el is None:
            continue
        accidentals_text = key_el.findtext('AccidentalCount')
        if accidentals_text is None:
            continue
        try:
            accidentals = int(accidentals_text)
        except ValueError:
            continue
        is_minor = (key_el.findtext('Mode') or 'Major').strip().lower() == 'minor'
        if (accidentals, is_minor) == last_sig:
            continue
        last_sig = (accidentals, is_minor)
        key, scale = key_name_for(accidentals, is_minor)
        events.append({'t': measure_times[measure_num], 'key': key, 'scale': scale})

    return {'version': 1, 'events': events} if events else None
