"""Hand-shape derivation (feedpak spec §6.5) for both source families.

gp2rs.py never populates `handShapes` in the RS XML it writes ("empty for
now" per its own comment) — song.parse_arrangement therefore always
returns an empty list, regardless of source format. This derives a
reasonable approximation from the chord data every GP source *does*
carry: consecutive chord hits sharing the same template id are grouped
into one contiguous hand-shape span (the fretting hand holds one shape
across a strumming/picking pattern, not a fresh shape per hit), ending at
the next differing chord or the group's own last sustain.

This is a heuristic, not authored data — GP has no explicit "hold this
shape until here" concept — but it is strictly better than the status
quo (always empty), and the ecosystem's own chord/handshape display
already treats these as advisory, not gameplay-critical.
"""

from __future__ import annotations

_DEFAULT_SUSTAIN = 0.5


def derive_handshapes(wire: dict) -> list[dict]:
    """wire is an arrangement dict already produced by
    song.arrangement_to_wire (has 'chords' and 'templates' keys)."""
    chords = sorted(wire.get('chords', []) or [], key=lambda c: c['t'])
    if not chords:
        return []
    templates = wire.get('templates', []) or []

    shapes: list[dict] = []
    i, n = 0, len(chords)
    while i < n:
        cid = chords[i]['id']
        j = i
        while j + 1 < n and chords[j + 1]['id'] == cid:
            j += 1

        start = chords[i]['t']
        last = chords[j]
        own_sustain = max((cn.get('sus', 0) or 0 for cn in last.get('notes', [])), default=0)
        end = last['t'] + (own_sustain or _DEFAULT_SUSTAIN)
        if j + 1 < n:
            end = min(end, chords[j + 1]['t'])

        arp = bool(templates[cid].get('arp')) if 0 <= cid < len(templates) else False
        shapes.append({
            'chord_id': cid,
            'start_time': round(start, 3),
            'end_time': round(max(end, start), 3),
            'arp': arp,
        })
        i = j + 1

    return shapes
