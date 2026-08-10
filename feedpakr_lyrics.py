"""Lyrics extraction for both GP source families.

Two independent sources, in order of trustworthiness:

- GPIF (.gpx/.gp) vocal tracks: gp2rs_gpx.convert_file already produces a
  precisely-timed `<vocals>` arrangement XML for any vocal track index it's
  given (via convert_vocal_track internally) — parse_vocals_xml() reads
  that back out. Timing here is exact (tied to real notes).
- GP3/GP4/GP5: pyguitarpro exposes only a single song-wide `song.lyrics`
  blob (one lyric line per starting measure, no per-syllable timing at
  all) — extract_gp345_lyrics() distributes its syllables evenly across
  each line's measure span using the song_timeline beat grid. This is a
  deliberate approximation (the source format has nothing better to
  offer); callers should surface that in their warnings, not hide it.

Both return the feedpak lyrics.json shape: a flat list of
{"t": float, "d": float, "w": str} syllable objects (spec §7.1) — a
trailing "-" on `w` joins to the next syllable, a trailing "+" marks the
last syllable of a line.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET


def is_vocals_xml(xml_path: str) -> bool:
    """Cheap peek at an arrangement XML's root tag — convert_file emits
    <vocals> for vocal tracks and <song> for every other track. The file
    starts with an XML declaration prolog (<?xml version="1.0" ?>) before
    the root element, so this checks for the tag appearing early rather
    than requiring it at position 0."""
    with open(xml_path, 'r', encoding='utf-8') as f:
        head = f.read(200)
    return '<vocals' in head


def parse_vocals_xml(xml_path: str) -> list[dict]:
    """Parse a gp2rs_gpx `<vocals>` arrangement XML into lyrics.json entries."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    entries = []
    for v in root.findall('vocal'):
        lyric = v.get('lyric', '')
        if not lyric:
            continue
        entries.append({
            't': float(v.get('time', 0)),
            'd': float(v.get('length', 0)),
            'w': lyric,
        })
    return entries


def parse_vocal_pitch_xml(xml_path: str) -> dict | None:
    """Extract vocal_pitch.json (spec §7.2) from the same `<vocals>` XML
    parse_vocals_xml reads — each `<vocal>` element already carries the
    sung MIDI pitch as its `note` attribute, so this needs no separate
    call into gp2rs_gpx's private per-syllable pitch sidecar function.
    `note="0"` marks a lyric-only rest (no real pitch, see convert_file's
    docstring) and is excluded, not zero-mapped."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    notes = []
    for v in root.findall('vocal'):
        try:
            midi = int(v.get('note', 0))
        except ValueError:
            continue
        if midi <= 0:
            continue
        notes.append({
            't': float(v.get('time', 0)),
            'd': float(v.get('length', 0)),
            'midi': midi,
        })
    return {'version': 1, 'notes': notes} if notes else None


def reconstruct_plain_text(entries: list[dict]) -> str:
    """Rebuild readable plain-text lyrics from feedpak lyrics.json entries
    (spec §7.1) — the inverse of the *shape* extract_gp345_lyrics() produces
    (parse_vocals_xml()'s GPIF output uses the same entry shape but writes
    `w` verbatim from the source XML, with no "-"/"+" markers of its own —
    this only round-trips markers a producer actually emitted). A trailing
    "-" on `w` joins directly onto the next entry (no space, mid-word
    split); a trailing "+" ends the current line. extract_gp345_lyrics()
    only ever adds "+" (line ends); a "-" it emits is a literal hyphen
    already present in the GP3-5 source lyric text, not a synthesized
    split — reconstructing that case glues the next word on with no space,
    a known minor rough edge for hyphenated source lyrics. Used to hand a
    pack's own already-extracted (possibly approximate) lyrics back to a
    forced aligner as plain text, e.g. the lyrics_sync handoff — never
    called during a normal build.
    """
    lines: list[str] = []
    current = ''
    pending_join = False
    for entry in entries:
        w = str((entry or {}).get('w', ''))
        if not w:
            continue
        is_join = w.endswith('-')
        is_line_end = w.endswith('+')
        if is_join or is_line_end:
            w = w[:-1]
        current = current + w if pending_join else (f'{current} {w}' if current else w)
        pending_join = is_join
        if is_line_end:
            lines.append(current)
            current = ''
    if current:
        lines.append(current)
    return '\n'.join(lines)


_SYLLABLE_SPLIT = re.compile(r'\s+')


def extract_gp345_lyrics(gp_song, track_indices: list[int], beats: list[dict]) -> list[dict] | None:
    """Best-effort lyrics for a GP3/4/5 source from song.lyrics.

    song.lyrics.trackChoice names which track the lyrics were authored
    against (pyguitarpro track index, 0-based); only used when that track
    is actually among the ones being imported, so the measure grid we're
    distributing onto really is the song being packed. Returns None when
    there's nothing usable — never an empty list (mirrors the "omit the
    key rather than emit a hollow one" convention used elsewhere).
    """
    lyrics = getattr(gp_song, 'lyrics', None)
    if lyrics is None or not lyrics.lines:
        return None
    if lyrics.trackChoice not in track_indices:
        return None

    lines = [
        (line.startingMeasure, line.lyrics.strip())
        for line in lyrics.lines
        if (line.lyrics or '').strip()
    ]
    if not lines:
        return None
    lines.sort(key=lambda pair: pair[0])

    # measure -> time, from the downbeats already computed for song_timeline
    # (ebeats carry `measure` only on downbeats; -1 elsewhere).
    measure_times = {b['measure']: b['time'] for b in beats if b.get('measure', -1) >= 0}
    if not measure_times:
        return None
    max_time = max(b['time'] for b in beats)

    def _time_for_measure(m: int) -> float:
        if m in measure_times:
            return measure_times[m]
        # Fall back to the nearest known measure at or before this one —
        # GP measure numbering has no gaps in ebeats, so this only matters
        # for a lyric line starting past the last downbeat we recorded.
        candidates = [t for mm, t in measure_times.items() if mm <= m]
        return max(candidates) if candidates else 0.0

    entries: list[dict] = []
    for i, (measure, text) in enumerate(lines):
        start = _time_for_measure(measure)
        end = (
            _time_for_measure(lines[i + 1][0])
            if i + 1 < len(lines) else max_time
        )
        syllables = [s for s in _SYLLABLE_SPLIT.split(text) if s]
        if not syllables:
            continue
        span = max(end - start, 0.01)
        step = span / len(syllables)
        for j, syl in enumerate(syllables):
            w = syl
            if j == len(syllables) - 1 and not w.endswith('-'):
                w = w + '+'
            entries.append({'t': start + j * step, 'd': step, 'w': w})

    return entries or None
