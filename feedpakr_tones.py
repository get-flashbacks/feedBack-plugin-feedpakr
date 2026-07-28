"""Tone-change extraction for both source families.

Neither source's convert path lands `tones` where `song.parse_arrangement`
can see it:

- GPIF: gp2rs_gpx.convert_file DOES inject a `<tonebase>`/`<tones>` block
  into the XML (via _collect_tone_events/_inject_tones) — but
  song.parse_arrangement never reads those elements, so the data is
  written and then silently dropped on the way to the wire format.
  parse_tones_xml() reads it back out directly.
- GP3/4/5: gp2rs.py never collects tone data at all (no equivalent of
  _collect_tone_events exists in that module). extract_gp345_tones()
  fills the gap by walking every selected track's `mixTableChange`
  events — deliberately across *all* selected tracks, not just the
  first one, unlike the legacy tabimport importer this project replaces.

Both return the wire shape documented in lib/tones.py::sloppak_tone_changes
and feedpak spec §6.9: {"base": str, "changes": [{"t": float, "name": str}]}
(base_rig/definitions are additive and left unset — GP carries no rig/gear
data to populate them with).
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

try:
    import gp2rs
except ImportError:  # pragma: no cover
    gp2rs = None

# GM program -> friendly tone name, for the families GP charts actually use
# (guitar 24-31, bass 32-39). Anything else gets a generic "Tone N" label
# rather than being silently dropped.
_GM_TONE_NAMES = {
    24: 'Nylon Guitar', 25: 'Steel Guitar', 26: 'Jazz Guitar', 27: 'Clean Guitar',
    28: 'Muted Guitar', 29: 'Overdriven Guitar', 30: 'Distortion Guitar',
    31: 'Guitar Harmonics',
    32: 'Acoustic Bass', 33: 'Fingered Bass', 34: 'Picked Bass',
    35: 'Fretless Bass', 36: 'Slap Bass 1', 37: 'Slap Bass 2',
    38: 'Synth Bass 1', 39: 'Synth Bass 2',
}


def _program_name(program: int) -> str:
    return _GM_TONE_NAMES.get(program, f'Tone {program}')


def parse_tones_xml(xml_path: str) -> dict | None:
    """Read a `<tonebase>`/`<tones>` block back out of a converted RS XML
    (gp2rs_gpx writes these for GPIF sources; gp2rs never does for GP3-5)."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return None
    root = tree.getroot()

    base_el = root.find('tonebase')
    base = (base_el.text or '').strip() if base_el is not None else ''

    changes: list[dict] = []
    tones_el = root.find('tones')
    if tones_el is not None:
        for tone in tones_el.findall('tone'):
            name = tone.get('name', '')
            time_attr = tone.get('time')
            if not name or time_attr is None:
                continue
            try:
                changes.append({'t': float(time_attr), 'name': name})
            except ValueError:
                continue

    if not base and not changes:
        return None
    return {'base': base or (changes[0]['name'] if changes else ''), 'changes': changes}


def extract_gp345_tones(gp_song, track_indices: list[int]) -> dict | None:
    """Best-effort tone changes for a GP3/4/5 source, scanning every
    selected track (not just the first, unlike the legacy importer)."""
    if gp2rs is None:
        return None

    tempo_map = gp2rs._build_tempo_map(gp_song)
    events: list[tuple[float, str]] = []

    for idx in track_indices:
        if idx >= len(gp_song.tracks):
            continue
        track = gp_song.tracks[idx]
        for measure in track.measures:
            for voice in measure.voices:
                for beat in voice.beats:
                    effect = getattr(beat, 'effect', None)
                    mtc = getattr(effect, 'mixTableChange', None) if effect else None
                    instrument = getattr(mtc, 'instrument', None) if mtc else None
                    value = getattr(instrument, 'value', None) if instrument else None
                    if value is None or value < 0:
                        continue
                    t = gp2rs._tick_to_seconds(beat.start, tempo_map)
                    events.append((t, _program_name(int(value))))

    if not events:
        return None

    events.sort(key=lambda e: e[0])
    # Deduplicate consecutive identical names (mirrors _collect_tone_events).
    deduped: list[tuple[float, str]] = []
    for t, name in events:
        if not deduped or deduped[-1][1] != name:
            deduped.append((t, name))

    return {
        'base': deduped[0][1],
        'changes': [{'t': t, 'name': name} for t, name in deduped],
    }
