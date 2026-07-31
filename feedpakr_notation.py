"""Notation sidecars (feedpak spec §7.6) for GPIF keys/piano tracks.

GPIF-only: gp2notation.py reads GPIF's own bar/voice/beat graph directly
and has no GP3/4/5 equivalent. Scoped to keys/piano tracks for the same
reason the plan calls out — that's where notation view actually matters
downstream (the piano/keys highway), and a full multi-instrument notation
pass is a much bigger undertaking than this phase's budget covers.

Only produces single-hand notation (no Piano LH/RH two-stave merge, even
when gp2rs_gpx._find_piano_pairs found a pair for the fretted/wire path)
— gp2notation.convert_track_to_notation supports a merged lh_raw_idx, but
wiring that up needs the same merge_map plumbing piano-pair handling
already carries elsewhere; left for a follow-up rather than doing it
half-right here.
"""

from __future__ import annotations

try:
    import gp2rs_gpx
except ImportError:  # pragma: no cover
    gp2rs_gpx = None

try:
    import gp2notation
except ImportError:  # pragma: no cover
    gp2notation = None


def raw_track_index_map(root) -> dict[int, int]:
    """filtered track index -> raw bar-column index, the same mapping
    convert_file computes internally as `filtered_to_raw` (gp2rs_gpx.py) —
    duplicated here because it's derived from _gpif_tracks(root), which
    this module already needs for string_pitches/name anyway."""
    raw_tracks = gp2rs_gpx._gpif_tracks(root)
    return {i: t['stave_columns'][0] for i, t in enumerate(raw_tracks)}


def convert_track_notation(
    gp_path: str,
    track_index: int,
    track_name: str,
    *,
    instrument: str = 'piano',
) -> dict | None:
    """Build a notation payload for one selected GPIF track, or None on
    any failure (caller treats this as best-effort, like everything else)."""
    if gp2rs_gpx is None or gp2notation is None:
        return None
    root = gp2rs_gpx._load_gpif(gp_path)
    raw_tracks = gp2rs_gpx._gpif_tracks(root)
    raw_map = {i: t['stave_columns'][0] for i, t in enumerate(raw_tracks)}
    if track_index not in raw_map or track_index >= len(raw_tracks):
        return None

    return gp2notation.convert_track_to_notation(
        root,
        raw_map[track_index],
        raw_tracks[track_index]['string_pitches'],
        instrument=instrument,
        track_name=track_name,
    )


convert_keys_track_notation = convert_track_notation
