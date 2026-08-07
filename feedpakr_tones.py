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

from defusedxml.ElementTree import parse, ParseError

try:
    import gp2rs
except ImportError:  # pragma: no cover
    gp2rs = None

try:
    import gp2rs_gpx
except ImportError:  # pragma: no cover
    gp2rs_gpx = None

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
        tree = parse(xml_path)
    except ParseError:
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


def _masterbar_start_times(masterbars, tempo_map, tempo_bpm: float) -> list[float]:
    """Cumulative start time (seconds) of each MasterBar, given a bar->bpm
    tempo_map (gp2rs_gpx._build_tempo_map's shape). Mirrors the same
    bar-duration math gp2rs_gpx._collect_tone_events uses internally, so a
    Sound-automation time and a <Bank>-tone time land on the same clock."""
    times: list[float] = []
    current_time = 0.0
    tempo_iter = iter(tempo_map)
    next_tempo_bar, next_tempo_bpm = next(tempo_iter, (999999, tempo_bpm))
    cur_tempo = tempo_bpm
    for mb_idx, mb in enumerate(masterbars):
        while mb_idx >= next_tempo_bar:
            cur_tempo = next_tempo_bpm
            next_tempo_bar, next_tempo_bpm = next(tempo_iter, (999999, cur_tempo))
        times.append(current_time)
        time_sig = mb.findtext('Time', '4/4')
        try:
            num_b, den_b = (int(x) for x in time_sig.split('/'))
        except ValueError:
            num_b, den_b = 4, 4
        current_time += num_b * (4.0 / den_b) * (60.0 / cur_tempo)
    return times


def extract_gpif_sound_changes(gp_path: str, track_index: int) -> dict | None:
    """GPIF-only — reads a *different* mechanism than parse_tones_xml: a
    track-level `<Automations><Automation><Type>Sound</Type>…` list, which
    swaps the MIDI/RSE instrument a track plays at a given bar (e.g. a keys
    track switching from piano to strings mid-song). Neither
    gp2rs_gpx._collect_tone_events (which only reads per-beat <Bank>, the
    older guitar/bass-oriented mechanism, and is only even called for
    non-keys tracks) nor anything else in the host reads this. Found
    against 'Bring Me To Life (J).gp': its Piano track switches to
    Ensemble/Violin at bar 13 and back to piano at bar 85 — a real,
    previously-unreported tone change.

    Returns the same {"base", "changes"} shape as parse_tones_xml /
    extract_gp345_tones (feedpak spec §6.9) — no `rig`/`base_rig`, since
    GP's RSE softsynth patches aren't portable rig data (no .sf2 ships with
    the source); a Reader gets the tone-change *names* honestly rather than
    an invented playable rig."""
    if gp2rs_gpx is None:
        return None
    try:
        root = gp2rs_gpx._load_gpif(gp_path)
        raw_tracks = gp2rs_gpx._gpif_tracks(root)
        if track_index >= len(raw_tracks):
            return None
        track_el = raw_tracks[track_index].get('_el')
        if track_el is None:
            return None
        autos_el = track_el.find('Automations')
        if autos_el is None:
            return None

        bar_events: list[tuple[int, str]] = []
        for a in autos_el.findall('Automation'):
            if (a.findtext('Type') or '') != 'Sound':
                continue
            bar_text = a.findtext('Bar')
            value = (a.findtext('Value') or '').strip()
            if bar_text is None or not value:
                continue
            try:
                bar_idx = int(bar_text)
            except ValueError:
                continue
            # Value is "Path;Name;Role" (e.g. "Orchestra/Strings/Violin;
            # Ensemble;Factory") — the display name is the middle segment.
            name = value.split(';')[1].strip() if ';' in value else value
            if name:
                bar_events.append((bar_idx, name))
        if not bar_events:
            return None
        bar_events.sort(key=lambda e: e[0])

        masterbars = list(root.find('MasterBars') or [])
        tempo_map = gp2rs_gpx._build_tempo_map(root)
        tempo_bpm = gp2rs_gpx._gpif_tempo(root)
        bar_times = _masterbar_start_times(masterbars, tempo_map, tempo_bpm)

        changes: list[dict] = []
        for bar_idx, name in bar_events:
            if changes and changes[-1]['name'] == name:
                continue  # dedupe consecutive identical names
            t = bar_times[bar_idx] if 0 <= bar_idx < len(bar_times) else 0.0
            changes.append({'t': round(t, 3), 'name': name})
        if not changes:
            return None
        return {'base': changes[0]['name'], 'changes': changes}
    except Exception:
        return None
