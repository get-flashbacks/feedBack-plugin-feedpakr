"""GP3-GP8 -> .feedpak import pipeline.

Two source families, both handled here:
  - GP3/GP4/GP5 (.gp3/.gp4/.gp5): parsed via pyguitarpro, converted via
    gp2rs.py.
  - GP6/GP7/GP8 (.gpx/.gp): parsed/converted via gp2rs_gpx.py (GPIF XML —
    pyguitarpro cannot read these at all). Known limitation, surfaced as a
    warning rather than hidden: GPIF repeat/volta expansion isn't
    implemented in the host core yet (gp2rs_gpx.convert_file's own
    docstring says so), so a repeated section's timing in song_timeline.json
    will not match an equivalent .gp5 import of the same song.

gp2rs.list_tracks / gp2rs.convert_file already dispatch to gp2rs_gpx
internally based on file extension, so most of this module is written
against gp2rs's uniform surface. The exceptions — vocal-track routing,
per-track capo, and piano LH/RH pairing — need format-specific handling
because pyguitarpro has no model for any of them on the GPIF side.

Every enrichment step below is best-effort: a failure degrades that one
feature and is recorded in the returned warnings list, never aborts the
whole import (see the project's "imports without errors, retaining as
much data as possible" requirement).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

try:
    import guitarpro
except ImportError:  # pragma: no cover - exercised only outside the host
    guitarpro = None

try:
    import gp2rs
except ImportError:  # pragma: no cover
    gp2rs = None

try:
    import gp2rs_gpx
except ImportError:  # pragma: no cover
    gp2rs_gpx = None

try:
    import song as song_mod
except ImportError:  # pragma: no cover
    song_mod = None

import feedpakr_pack as pack
import feedpakr_validate as validate
import feedpakr_lyrics as lyrics_mod
import feedpakr_audio as audio_mod
import feedpakr_tones as tones_mod
import feedpakr_keys as keys_mod
import feedpakr_handshapes as handshapes_mod
import feedpakr_notation as notation_mod

log = logging.getLogger('feedBack.plugin.feedpakr')
_load_sibling = None

_GP345_EXTS = {'.gp3', '.gp4', '.gp5'}
_GPX_EXTS = {'.gpx', '.gp'}  # GP6 / GP7-8, GPIF XML path

ALLOWED_ARRANGEMENT_NAMES = {'Lead', 'Rhythm', 'Bass', 'Drums', 'Keys', 'Vocals'}


class UnsupportedFormatError(Exception):
    """Raised for a file extension feedpakr does not handle."""


def configure_sibling_loader(load_sibling) -> None:
    """Provide the host's namespaced sibling-module loader."""
    global _load_sibling
    _load_sibling = load_sibling


def _require_core() -> None:
    if gp2rs is None or song_mod is None:
        raise RuntimeError(
            'feedpakr requires the feedBack host core lib (gp2rs, song) on '
            'sys.path — it must run inside the feedBack app.'
        )


def _is_gpif(gp_path: str) -> bool:
    return Path(gp_path).suffix.lower() in _GPX_EXTS


def _check_extension(gp_path: str) -> None:
    ext = Path(gp_path).suffix.lower()
    if ext not in _GP345_EXTS and ext not in _GPX_EXTS:
        raise UnsupportedFormatError(f'Unsupported file extension: {ext}')
    if ext in _GPX_EXTS and gp2rs_gpx is None:
        raise UnsupportedFormatError(
            f'{ext} needs gp2rs_gpx, which is not available on this host.'
        )
    if ext in _GP345_EXTS and guitarpro is None:
        raise UnsupportedFormatError(
            f'{ext} needs pyguitarpro, which is not available on this host.'
        )


# ── Parsing / track listing ─────────────────────────────────────────────────

def parse_gp(gp_path: str) -> dict:
    """Parse a GP file and return metadata + track list for the upload UI."""
    _require_core()
    _check_extension(gp_path)

    tracks = gp2rs.list_tracks(gp_path)

    if _is_gpif(gp_path):
        root = gp2rs_gpx._load_gpif(gp_path)
        # _auto_select_gpx needs the private raw track shape (midi_program,
        # string_pitches, …) from _gpif_tracks — the public list_tracks()
        # dicts above trade those for UI-friendly fields (is_vocal, is_piano)
        # and don't carry what the selector reads.
        raw_tracks = gp2rs_gpx._gpif_tracks(root)
        _, auto_names = gp2rs_gpx._auto_select_gpx(raw_tracks)
        score = root.find('Score')
        title = (score.findtext('Title') or '').strip() if score is not None else ''
        artist = (score.findtext('Artist') or '').strip() if score is not None else ''
        album = (score.findtext('Album') or '').strip() if score is not None else ''
        tempo = gp2rs_gpx._gpif_tempo(root)
    else:
        gp_song = guitarpro.parse(gp_path)
        _, auto_names = gp2rs.auto_select_tracks(gp_path)
        title, artist, album, tempo = gp_song.title, gp_song.artist, gp_song.album, gp_song.tempo

    for t in tracks:
        auto_role = auto_names.get(t['index'])
        t['auto_name'] = auto_role or ''
        t['auto_selected'] = auto_role is not None

    is_gpif = _is_gpif(gp_path)
    has_embedded_audio = False
    if is_gpif:
        try:
            import gp8_audio_sync
            has_embedded_audio = gp8_audio_sync.has_embedded_audio(gp_path)
        except ImportError:
            pass

    return {
        'title': title or Path(gp_path).stem,
        'artist': artist or '',
        'album': album or '',
        'tempo': tempo,
        'tracks': tracks,
        # 'gpif' (GP6/7/8): no MIDI synthesis (pyguitarpro can't read
        # these), embedded/sync audio only. 'gp345': all three modes.
        'format': 'gpif' if is_gpif else 'gp345',
        'has_embedded_audio': has_embedded_audio,
    }


# ── Capo ─────────────────────────────────────────────────────────────────

def _capo_for_track(gp_song, track_index: int) -> int:
    """GP3/4/5 per-track capo fret. pyguitarpro exposes it as Track.offset —
    gp2rs never reads it (it hardcodes <capo>0</capo> in the RS XML), so
    feedpakr overrides the wire value with the real one post-conversion."""
    try:
        return max(0, int(gp_song.tracks[track_index].offset or 0))
    except (IndexError, AttributeError, TypeError, ValueError):
        return 0


def _gpif_capo_lookup(gp_path: str) -> dict[int, int]:
    """Same fix for GPIF sources, whose capo lives at
    Track/Staves/Staff/Properties/Property[@name='CapoFret']/Fret in the
    GPIF XML — gp2rs_gpx doesn't read it either. Returns {track_index: capo}
    for every track that actually declares a nonzero capo (absent = 0)."""
    result: dict[int, int] = {}
    try:
        root = gp2rs_gpx._load_gpif(gp_path)
        raw_tracks = gp2rs_gpx._gpif_tracks(root)
        for i, t in enumerate(raw_tracks):
            el = t.get('_el')
            if el is None:
                continue
            fret_el = el.find(".//Staves/Staff/Properties/Property[@name='CapoFret']/Fret")
            if fret_el is not None and (fret_el.text or '').strip():
                try:
                    capo = int(float(fret_el.text))
                except ValueError:
                    continue
                if capo > 0:
                    result[i] = capo
    except Exception:
        log.warning('capo: GPIF capo lookup failed', exc_info=True)
    return result


def _extract_authors(gp_path: str) -> list[str]:
    """Extract authors/tabber information from GP file. Returns a list of
    author names (or empty list if none found)."""
    authors: list[str] = []
    try:
        if _is_gpif(gp_path):
            root = gp2rs_gpx._load_gpif(gp_path)
            score = root.find('Score')
            if score is not None:
                tabber = (score.findtext('Tabber') or '').strip()
                if tabber:
                    authors.append(tabber)
        else:
            gp_song = guitarpro.parse(gp_path)
            if gp_song and hasattr(gp_song, 'tab') and gp_song.tab:
                tabber = str(gp_song.tab).strip()
                if tabber:
                    authors.append(tabber)
    except Exception:
        log.warning('authors: extraction failed', exc_info=True)
    return authors


def _gpif_has_repeat_markup(gp_path: str) -> bool:
    """True if the score uses repeat brackets / volta endings that GPIF
    conversion (unlike the GP3-5 path) does not currently expand."""
    try:
        root = gp2rs_gpx._load_gpif(gp_path)
        masterbars = root.find('MasterBars') or []
        return any(
            mb.find('Repeat') is not None or mb.find('AlternateEndings') is not None
            for mb in masterbars
        )
    except Exception:
        return False


def _gpif_drumtab_from_wire(wire: dict, arrangement_name: str) -> tuple[dict, dict[int, int]]:
    """Turn gp2rs_gpx's lossless percussion MIDI encoding into drum-tab.

    gp2rs_gpx represents every GPIF drum hit as ``string = midi // 24`` and
    ``fret = midi % 24`` in its intermediate arrangement XML.  Decode that
    carrier here and route the MIDI note through the host's canonical drum
    vocabulary.  Velocity/ghost/flam are not present in the intermediate XML,
    so those optional drum-tab fields are deliberately omitted rather than
    fabricated.
    """
    if _load_sibling is None:
        raise RuntimeError('feedpakr sibling loader is not configured')
    drums_mod = _load_sibling('drums')

    hits: list[dict] = []
    pieces_seen: dict[str, str] = {}
    unmapped: dict[int, int] = {}

    def add_hit(note: dict, time: float) -> None:
        try:
            midi = int(note['s']) * 24 + int(note['f'])
        except (KeyError, TypeError, ValueError):
            return
        piece = drums_mod.midi_to_piece(midi)
        if piece is None:
            unmapped[midi] = unmapped.get(midi, 0) + 1
            return
        hits.append({'t': round(float(time), 3), 'p': piece})
        pieces_seen.setdefault(piece, piece.replace('_', ' ').title())

    for note in wire.get('notes', []):
        add_hit(note, note.get('t', 0.0))
    for chord in wire.get('chords', []):
        for note in chord.get('notes', []):
            add_hit(note, chord.get('t', 0.0))

    hits.sort(key=lambda hit: hit['t'])
    return ({
        'version': 1,
        'name': arrangement_name,
        'kit': [{'id': piece, 'name': label} for piece, label in pieces_seen.items()],
        'hits': hits,
    }, unmapped)


def _gpif_played_chord_names(root, track: dict) -> dict[tuple, str]:
    """Map played fret shapes to the chord names displayed by Guitar Pro.

    GPIF's DiagramCollection is not necessarily a voicing dictionary.  In
    real GP8 files, items with ``ShowDiagram=false`` often all contain the
    same placeholder frets; the authoritative association is the beat's
    ``<Chord>item-id</Chord>`` reference.  Rebuild the played shape from that
    beat's notes so it keys exactly like gp2rs_gpx's emitted templates.
    """
    items = {
        item.get('id'): (item.get('name') or '').strip()
        for item in track['_el'].findall(
            './/Property[@name="DiagramCollection"]/Items/Item')
    }
    if not any(items.values()):
        return {}

    masterbars = list(root.find('MasterBars') or [])
    bars = {el.get('id'): el for el in (root.find('Bars') or [])}
    voices = {el.get('id'): el for el in (root.find('Voices') or [])}
    beats = {el.get('id'): el for el in (root.find('Beats') or [])}
    notes = {el.get('id'): el for el in (root.find('Notes') or [])}
    names_by_shape: dict[tuple, str] = {}

    for column, pitches in zip(track['stave_columns'], track['stave_pitches'], strict=True):
        order = sorted(range(len(pitches)), key=lambda i: (pitches[i], i))
        for masterbar in masterbars:
            bar_ids = masterbar.findtext('Bars', '').split()
            if column >= len(bar_ids):
                continue
            bar = bars.get(bar_ids[column])
            if bar is None:
                continue
            for voice_id in bar.findtext('Voices', '').split():
                voice = voices.get(voice_id)
                if voice is None:
                    continue
                for beat_id in voice.findtext('Beats', '').split():
                    beat = beats.get(beat_id)
                    if beat is None:
                        continue
                    chord_name = items.get((beat.findtext('Chord') or '').strip(), '')
                    if not chord_name:
                        continue
                    played: dict[int, int] = {}
                    for note_id in beat.findtext('Notes', '').split():
                        note = notes.get(note_id)
                        if note is None or gp2rs_gpx._note_is_tie(note):
                            continue
                        props = {p.get('name'): p for p in note.findall('.//Property')}
                        try:
                            gp_string = int(props['String'].findtext('String'))
                            fret = int(props['Fret'].findtext('Fret'))
                            rs_string = order.index(gp_string)
                        except (KeyError, TypeError, ValueError):
                            continue
                        played[rs_string] = fret
                    if not played:
                        continue
                    width = max(6, max(played) + 1)
                    frets = [-1] * width
                    for string, fret in played.items():
                        frets[string] = fret
                    names_by_shape.setdefault(tuple(frets), chord_name)
    return names_by_shape


# ── Track ordering (needed to map an output XML back to its GP track) ──────

def _output_track_order(gp_path: str, track_indices: list[int], names: dict[int, str]) -> list[int]:
    """The order gp2rs.convert_file will actually emit one XML per entry.

    Identical to track_indices for GP3-5 (gp2rs.py never drops a requested
    track). For GPIF sources, convert_file internally removes any Piano LH
    track consumed by a same-stem RH pairing (_find_piano_pairs) — this
    computes that same filtered order up front (from the same public
    list_tracks() shape _find_piano_pairs actually reads) so the per-track
    capo/lyrics loop can zip() against the real output list.
    """
    if not _is_gpif(gp_path):
        return list(track_indices)
    tracks = gp2rs.list_tracks(gp_path)
    filtered, _merge_map = gp2rs_gpx._find_piano_pairs(track_indices, tracks, names)
    return filtered


# ── song_timeline ───────────────────────────────────────────────────────────

def _load_song_meta(xml_dir: Path):
    """Load song-level data (beats/sections/duration) from the converted RS
    XML via the same song.load_song() the rest of the host uses — these
    times are playback-schedule-accurate for GP3-5 (repeat/D.S./D.C.
    expansion already applied by gp2rs); for GPIF sources they reflect a
    single, unexpanded pass (see the module docstring). Returns None on
    failure (caller degrades gracefully)."""
    try:
        return song_mod.load_song(str(xml_dir))
    except Exception:
        log.warning('song_timeline: load_song failed', exc_info=True)
        return None


def _song_timeline_from_meta(loaded) -> dict | None:
    if loaded is None or (not loaded.beats and not loaded.sections):
        return None

    timeline: dict = {'version': 1}
    if loaded.beats:
        timeline['beats'] = [
            {'time': b.time, 'measure': b.measure} for b in loaded.beats
        ]
    if loaded.sections:
        timeline['sections'] = [
            {'name': s.name, 'number': s.number, 'time': s.start_time}
            for s in loaded.sections
        ]
    return timeline


def _merge_arrangement_wires(base: dict, extra: dict) -> dict:
    """Merge a second arrangement wire payload into ``base`` in place.

    Chord template ids are local to each source arrangement, so references in
    both chord occurrences and handshapes must be shifted before appending.
    """
    template_offset = len(base.get('templates', []))
    base.setdefault('templates', []).extend(extra.get('templates', []))

    def remap_chord_refs(container: dict) -> None:
        for chord in container.get('chords', []):
            if isinstance(chord.get('id'), int) and chord['id'] >= 0:
                chord['id'] += template_offset
        for shape in container.get('handshapes', []):
            if isinstance(shape.get('chord_id'), int) and shape['chord_id'] >= 0:
                shape['chord_id'] += template_offset

    for chord in extra.get('chords', []):
        copied = dict(chord)
        remap_chord_refs({'chords': [copied]})
        base.setdefault('chords', []).append(copied)
    for shape in extra.get('handshapes', []):
        copied = dict(shape)
        remap_chord_refs({'handshapes': [copied]})
        base.setdefault('handshapes', []).append(copied)

    base.setdefault('notes', []).extend(extra.get('notes', []))
    base.setdefault('anchors', []).extend(extra.get('anchors', []))
    for key in ('notes', 'chords'):
        base[key].sort(key=lambda item: item.get('t', 0))
    for key, time_key in (('anchors', 'time'), ('handshapes', 'start_time')):
        unique = {json.dumps(item, sort_keys=True): item for item in base.get(key, [])}
        base[key] = sorted(unique.values(), key=lambda item: item.get(time_key, 0))

    for phrase in extra.get('phrases', []):
        for level in phrase.get('levels', []):
            remap_chord_refs(level)
        match = next((item for item in base.get('phrases', [])
                      if item.get('start_time') == phrase.get('start_time')
                      and item.get('end_time') == phrase.get('end_time')), None)
        if match is None:
            base.setdefault('phrases', []).append(phrase)
            continue
        match['max_difficulty'] = max(match.get('max_difficulty', 0), phrase.get('max_difficulty', 0))
        by_difficulty = {level.get('difficulty'): level for level in match.get('levels', [])}
        for level in phrase.get('levels', []):
            target = by_difficulty.get(level.get('difficulty'))
            if target is None:
                match.setdefault('levels', []).append(level)
                continue
            for key in ('notes', 'chords', 'anchors', 'handshapes'):
                target.setdefault(key, []).extend(level.get(key, []))
    if base.get('phrases'):
        base['phrases'].sort(key=lambda item: item.get('start_time', 0))

    combined_tempos = base.get('tempos', []) + extra.get('tempos', [])
    if combined_tempos:
        unique = {json.dumps(item, sort_keys=True): item for item in combined_tempos}
        base['tempos'] = sorted(unique.values(), key=lambda item: item.get('time', 0))

    if extra.get('tones'):
        if not base.get('tones'):
            base['tones'] = extra['tones']
        else:
            changes = base['tones'].get('changes', []) + extra['tones'].get('changes', [])
            unique = {json.dumps(item, sort_keys=True): item for item in changes}
            base['tones']['changes'] = sorted(unique.values(), key=lambda item: item.get('time', 0))
    return base


def _combine_same_name_arrangements(entries: list[dict], files: dict[str, dict], warnings: list[str]) -> None:
    """Combine compatible regular arrangements sharing a display name."""
    kept: dict[str, dict] = {}
    remove: list[dict] = []
    for entry in entries:
        if 'file' not in entry:
            continue
        key = (entry.get('name') or '').strip().casefold()
        if not key:
            continue
        prior = kept.get(key)
        if prior is None:
            kept[key] = entry
            continue
        compatible = (
            prior.get('tuning') == entry.get('tuning')
            and prior.get('capo', 0) == entry.get('capo', 0)
            and 'notation' not in prior and 'notation' not in entry
        )
        if not compatible:
            warnings.append(
                f'Could not combine same-name arrangement "{entry.get("name")}" because '
                'its tuning, capo, or notation mode differs.'
            )
            continue
        prior_file = Path(prior['file']).name
        extra_file = Path(entry['file']).name
        _merge_arrangement_wires(files[prior_file], files[extra_file])
        del files[extra_file]
        remove.append(entry)
    for entry in remove:
        entries.remove(entry)
    if remove:
        warnings.append(f'Combined {len(remove)} same-name track(s) into existing arrangements.')


# ── Audio ────────────────────────────────────────────────────────────────

_AUDIO_MODES = {'midi', 'embedded', 'sync', 'none'}


def _resolve_audio(
    gp_path: str,
    track_indices: list[int],
    audio_mode: str,
    user_audio_path: str | None,
    tmp_dir: Path,
    warnings: list[str],
    report,
    existing_pack: dict | None = None,
) -> tuple[str | None, float]:
    """Returns (audio_path, offset). audio_path is None on any failure —
    callers treat that as the §5.3.2 authoring-intermediate carve-out, not
    a hard error.

    For audio_mode == 'existing_pack', audio_path is the source pack's
    'full' mixdown when it has one (None if it only has separated stems —
    those are packed separately by the caller via existing_pack['stems'],
    not through this return value)."""
    if audio_mode == 'none':
        report('Audio skipped (unchecked).', 15)
        return None, 0.0

    if audio_mode == 'midi':
        if _is_gpif(gp_path):
            warnings.append('Audio skipped: MIDI synthesis needs pyguitarpro, which cannot read GP6/7/8 files.')
            return None, 0.0
        audio_base = str(tmp_dir / 'audio')
        path, offset, err = audio_mod.synth_midi_audio(gp_path, track_indices, audio_base)
        if err:
            warnings.append(f'Audio skipped: {err}')
        return path, offset

    if audio_mode == 'embedded':
        path, offset, err = audio_mod.extract_embedded_audio(gp_path, str(tmp_dir))
        if err:
            warnings.append(f'Audio skipped: {err}')
        return path, offset

    if audio_mode == 'sync':
        if not user_audio_path or not Path(user_audio_path).exists():
            warnings.append('Audio skipped: no audio file was attached.')
            return None, 0.0
        report('Aligning audio to the chart…', 15)
        offset, _points, err = audio_mod.autosync_audio(gp_path, user_audio_path)
        if err:
            warnings.append(f'Autosync failed ({err}) — using the attached audio with a zero offset.')
            offset = 0.0
        normalized, terr = audio_mod.transcode_to_ogg(
            user_audio_path, str(tmp_dir / 'audio.ogg'),
        )
        if terr:
            warnings.append(f'Audio skipped: {terr}')
            return None, 0.0
        return normalized, offset

    if audio_mode == 'existing_pack':
        if not existing_pack or not existing_pack.get('sync_reference_path'):
            warnings.append('Audio skipped: no existing pack was attached.')
            return None, 0.0
        report('Aligning audio to the chart…', 15)
        offset, _points, err = audio_mod.autosync_audio(
            gp_path, existing_pack['sync_reference_path'],
        )
        if err:
            warnings.append(f'Autosync failed ({err}) — using the existing pack\'s audio with a zero offset.')
            offset = 0.0
        return existing_pack.get('full_mix_path'), offset

    warnings.append(f'Audio skipped: unknown audio mode {audio_mode!r}.')
    return None, 0.0


# ── Main pipeline ────────────────────────────────────────────────────────

def build_feedpak(
    gp_path: str,
    *,
    track_indices: list[int],
    arrangement_names: dict[int, str],
    title: str = '',
    artist: str = '',
    album: str = '',
    year: int | None = None,
    album_artist: str = '',
    track: int | None = None,
    disc: int | None = None,
    genres: list[str] | None = None,
    mbid: str = '',
    isrc: str = '',
    language: str = '',
    authors: list[str] | None = None,
    notation_track_indices: set[int] | None = None,
    combine_same_name: bool = False,
    audio_mode: str = 'midi',
    user_audio_path: str | None = None,
    existing_pack: dict | None = None,
    cover_path: str | None = None,
    report=lambda stage, pct: None,
) -> dict:
    """Run the full GP3-8 -> .feedpak pipeline. Returns:

        {
          'bytes': <the .feedpak file content>,
          'warnings': [str, ...],
          'validation': {part: [errors], ...},  # empty dict = fully valid
          'title': str, 'artist': str, 'album': str, 'duration': float,
          'arrangement_count': int,
        }

    Raises UnsupportedFormatError / RuntimeError only for conditions that
    make the whole import meaningless (unreadable file, no tracks
    selected) — everything else degrades into `warnings`.

    existing_pack (audio_mode == 'existing_pack' only): the dict returned
    by feedpakr_upgrade.extract_pack_assets() — re-imports the GP chart
    while reusing a previously-uploaded pack's audio/stems/cover
    byte-for-byte, re-aligned to the new chart via autosync. When
    cover_path isn't separately supplied, existing_pack's cover is used.
    """
    _require_core()
    _check_extension(gp_path)

    if not track_indices:
        raise RuntimeError('No tracks selected.')

    if not cover_path and existing_pack and existing_pack.get('cover_path'):
        cover_path = existing_pack['cover_path']

    is_gpif = _is_gpif(gp_path)
    warnings: list[str] = []
    if is_gpif and _gpif_has_repeat_markup(gp_path):
        warnings.append(
            'This file uses repeats/alternate endings, which GP6/7/8 import '
            'does not yet expand — section and beat timing after the first '
            'repeated bar will drift from a real performance.'
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix='feedpakr_build_'))
    try:
        report('Parsing Guitar Pro file…', 5)
        parsed = parse_gp(gp_path)
        use_title = (title or '').strip() or parsed['title']
        use_artist = (artist or '').strip() or parsed['artist']
        use_album = (album or '').strip() or parsed['album']

        gp_song = None if is_gpif else guitarpro.parse(gp_path)
        gpif_root = gp2rs_gpx._load_gpif(gp_path) if is_gpif else None
        gpif_tracks = gp2rs_gpx._gpif_tracks(gpif_root) if is_gpif else []
        gpif_capo = _gpif_capo_lookup(gp_path) if is_gpif else {}

        names = {idx: arrangement_names.get(idx, '') for idx in track_indices}
        output_order = _output_track_order(gp_path, track_indices, names)

        # Resolve audio (and its sync offset) BEFORE converting the chart:
        # convert_file's own audio_offset param is what actually shifts note
        # times to line up with the audio (see its docstring, "Seconds to add
        # for audio sync") — there's no separate manifest-level offset field
        # anywhere in this pipeline (feedpakr_pack.py has none), so an offset
        # computed here has nowhere else to go. Previously this ran AFTER
        # convert_file and the resulting audio_offset was never fed back in
        # at all — 'embedded' mode's GP8 lead-in offset and 'sync' mode's
        # autosync offset were both silently discarded, leaving the chart
        # and audio measurably out of alignment whenever either wasn't 0.
        report('Generating audio…', 10)
        audio_path, audio_offset = _resolve_audio(
            gp_path, track_indices, audio_mode, user_audio_path, tmp_dir, warnings, report,
            existing_pack=existing_pack,
        )
        # 'existing_pack' with only separated stems and no 'full' mixdown
        # legitimately returns audio_path=None from _resolve_audio — the
        # real audio lives in existing_pack['stems'], packed further down.
        # Only treat a None audio_path as fatal when there's truly nothing
        # to fall back on.
        has_existing_pack_stems = bool(
            audio_mode == 'existing_pack' and existing_pack and existing_pack.get('stems')
        )
        if audio_mode != 'none' and audio_path is None and not has_existing_pack_stems:
            detail = warnings[-1] if warnings else 'Audio could not be produced.'
            raise RuntimeError(detail)

        report('Converting tracks to arrangement XML…', 20)
        xml_dir = tmp_dir / 'xml'
        xml_paths = gp2rs.convert_file(
            gp_path, str(xml_dir),
            track_indices=track_indices,
            arrangement_names=names,
            audio_offset=audio_offset,
        )
        if not xml_paths:
            raise RuntimeError('No arrangements produced from the selected tracks.')
        if len(xml_paths) != len(output_order):
            # Piano-pair prediction didn't match reality (e.g. a merge
            # heuristic edge case) — fidelity enrichment below degrades
            # per-arrangement already, but the capo/lyrics track mapping
            # can't be trusted, so skip it rather than silently mislabel.
            warnings.append(
                'Could not reliably map converted arrangements back to '
                'source tracks — capo may be incorrect for this import.'
            )
            output_order = [None] * len(xml_paths)

        drum_indices: set[int] = {
            t['index'] for t in parsed['tracks']
            if t['is_drums'] and t['index'] in track_indices
        }

        gp345_tones = None
        if not is_gpif and gp_song is not None:
            try:
                gp345_tones = tones_mod.extract_gp345_tones(gp_song, track_indices)
            except Exception as e:
                warnings.append(f'Tone extraction failed: {e}')

        track_by_index = {t['index']: t for t in parsed['tracks']}

        report('Reading arrangement data…', 55)
        arrangement_entries: list[dict] = []
        arrangement_files: dict[str, dict] = {}
        drum_tab_files: dict[str, dict] = {}
        notation_files: dict[str, dict] = {}
        taken_ids: set[str] = set()
        lyrics_entries: list[dict] | None = None
        vocal_pitch_data: dict | None = None

        for idx, xml_path in zip(output_order, xml_paths):
            if lyrics_mod.is_vocals_xml(xml_path):
                try:
                    entries = lyrics_mod.parse_vocals_xml(xml_path)
                    if entries:
                        lyrics_entries = entries
                    vocal_pitch_data = lyrics_mod.parse_vocal_pitch_xml(xml_path)
                except Exception as e:
                    warnings.append(f'Lyrics extraction failed for a vocal track: {e}')
                continue

            if idx is not None and idx in drum_indices and not is_gpif:
                try:
                    drum_name = names.get(idx) or 'Drums'
                    drum_tab = gp2rs.convert_drum_track_to_drumtab(
                        gp_song, idx, arrangement_name=drum_name,
                    )
                    arr_id = pack.arrangement_id_for(drum_tab.get('name') or drum_name, taken_ids)
                    dt_filename = f'drum_tab_{arr_id}.json'
                    drum_tab_files[dt_filename] = drum_tab
                    arrangement_entries.append({
                        'id': arr_id,
                        'name': drum_tab.get('name', drum_name),
                        'type': 'drums',
                        'drum_tab': dt_filename,
                    })
                    warnings.append(
                        f'"{drum_tab.get("name", drum_name)}" was packed as a proper drum '
                        'arrangement (drum_tab.json) rather than fret-encoded notes — the '
                        'library index may not surface it correctly yet (feedBack#1027), '
                        'a known host limitation, not a fault in this pack.'
                    )
                    continue
                except Exception as e:
                    warnings.append(
                        f'Drum tab conversion failed for a track, falling back to fretted '
                        f'encoding: {e}'
                    )
                    # fall through — xml_path is still the fretted drum XML gp2rs already wrote

            try:
                arr = song_mod.parse_arrangement(xml_path)
            except Exception as e:
                warnings.append(f'Skipped an arrangement — failed to read {Path(xml_path).name}: {e}')
                continue

            # capo: neither gp2rs nor gp2rs_gpx read GP's own per-track capo
            # (gp2rs hardcodes <capo>0</capo>) — override with the real value.
            if idx is not None:
                arr.capo = gpif_capo.get(idx, 0) if is_gpif else _capo_for_track(gp_song, idx)

            wire = song_mod.arrangement_to_wire(arr)

            if is_gpif and idx is not None:
                try:
                    chord_names = _gpif_played_chord_names(gpif_root, gpif_tracks[idx])
                    for template in wire.get('templates', []):
                        name = chord_names.get(tuple(template.get('frets', [])))
                        if name:
                            template['name'] = name
                            template['displayName'] = name
                except Exception as e:
                    warnings.append(f'Chord-name extraction failed for {arr.name}: {e}')

            if idx is not None and idx in drum_indices:
                try:
                    drum_name = names.get(idx) or arr.name or 'Drums'
                    drum_tab, unmapped = _gpif_drumtab_from_wire(wire, drum_name)
                    if not drum_tab['hits']:
                        raise ValueError('no supported drum hits were decoded')
                    arr_id = pack.arrangement_id_for(drum_name, taken_ids)
                    dt_filename = f'drum_tab_{arr_id}.json'
                    drum_tab_files[dt_filename] = drum_tab
                    arrangement_entries.append({
                        'id': arr_id,
                        'name': drum_name,
                        'type': 'drums',
                        'drum_tab': dt_filename,
                    })
                    if unmapped:
                        count = sum(unmapped.values())
                        midis = ', '.join(str(m) for m in sorted(unmapped))
                        warnings.append(
                            f'{count} unsupported percussion hit(s) in "{drum_name}" '
                            f'were skipped (MIDI: {midis}).'
                        )
                    continue
                except Exception as e:
                    warnings.append(
                        'GPIF drum-tab conversion failed for a track, falling back '
                        f'to fretted encoding: {e}'
                    )

            if not wire.get('handshapes') and wire.get('chords'):
                try:
                    wire['handshapes'] = handshapes_mod.derive_handshapes(wire)
                except Exception as e:
                    warnings.append(f'Hand-shape derivation failed for {arr.name}: {e}')

            # Known host gap in BOTH gp2rs.py and gp2rs_gpx.py's chord-template
            # builders (not gp2rs_gpx-only, despite this warning's original
            # wording — confirmed by reproducing it on a plain .gp5 piano
            # track, which never touches gp2rs_gpx): when a chord occurrence
            # has two notes on the same string, only the last one survives
            # into that template's single-fret-per-string diagram summary.
            # Real for piano/keys/drum tracks (their string index is a MIDI
            # bucket — string = midi // 24 — so two genuinely different
            # pitches routinely collide on the same "string"); essentially
            # impossible for guitar/bass, where a same-string collision would
            # mean two different frets fingered on one physical string at
            # once. The per-occurrence note data itself (what's actually
            # played) is unaffected — chord.notes always keeps every note —
            # this only makes the auto-generated chord diagram/name preview
            # incomplete for that shape.
            same_string_collisions = sum(
                1 for c in wire.get('chords', [])
                if len({n['s'] for n in c.get('notes', [])}) < len(c.get('notes', []))
            )
            if same_string_collisions:
                warnings.append(
                    f'{same_string_collisions} chord(s) in "{arr.name}" have two notes on '
                    'the same string — the auto-generated chord-diagram summary only shows '
                    'one of them (a known host limitation, most common on piano/keys tracks), '
                    'but the actual playable note data for both is intact.'
                )

            try:
                tones = tones_mod.parse_tones_xml(xml_path) if is_gpif else gp345_tones
                # GPIF instrument/patch automation (§6.9) — a different GP
                # mechanism than <Bank> (e.g. a keys track switching piano
                # -> strings mid-song); prefer it when present since <Bank>
                # is rarely used and doesn't apply to non-guitar tracks at
                # all (gp2rs_gpx never even collects it for keys tracks).
                if is_gpif and idx is not None:
                    sound_changes = tones_mod.extract_gpif_sound_changes(gp_path, idx)
                    if sound_changes:
                        tones = sound_changes
                if tones:
                    wire['tones'] = tones
            except Exception as e:
                warnings.append(f'Tone extraction failed for {arr.name}: {e}')

            arr_id = pack.arrangement_id_for(arr.name, taken_ids)
            filename = f'{arr_id}.json'
            arrangement_files[filename] = wire
            entry = {
                'id': arr_id,
                'name': arr.name,
                'file': f'arrangements/{filename}',
                'tuning': list(arr.tuning),
                'capo': arr.capo,
            }

            track_info = track_by_index.get(idx, {}) if idx is not None else {}
            wants_notation = (
                is_gpif and idx is not None
                and (
                    idx in notation_track_indices
                    if notation_track_indices is not None
                    else bool(track_info.get('is_piano'))
                )
            )
            if wants_notation:
                try:
                    notation = notation_mod.convert_track_notation(
                        gp_path,
                        idx,
                        track_info.get('name') or arr.name,
                        instrument='piano' if track_info.get('is_piano') else 'melodic',
                    )
                    if notation:
                        notation_filename = f'notation_{arr_id}.json'
                        notation_files[notation_filename] = notation
                        entry['notation'] = notation_filename
                except Exception as e:
                    warnings.append(f'Notation extraction failed for {arr.name}: {e}')

            arrangement_entries.append(entry)

        if combine_same_name:
            _combine_same_name_arrangements(arrangement_entries, arrangement_files, warnings)

        if not arrangement_entries:
            raise RuntimeError('None of the selected tracks could be converted.')

        report('Building timeline (sections, beats)…', 68)
        song_meta = _load_song_meta(xml_dir)
        song_timeline = _song_timeline_from_meta(song_meta)
        if song_timeline is None:
            warnings.append('No sections/beats found in the source file.')
        duration = float(song_meta.song_length) if song_meta else 0.0

        # Sanity check: audio duration must match chart duration (allow ±5% tolerance for rounding).
        if audio_path and duration > 0:
            audio_duration = audio_mod.get_audio_duration(audio_path)
            if audio_duration is not None:
                # convert_file shifts chart events by audio_offset, so compare
                # the audio endpoint with the aligned chart endpoint rather
                # than the unshifted source duration. This avoids flagging a
                # legitimate recording lead-in as the wrong song.
                aligned_duration = max(0.0, duration + audio_offset)
                drift = abs(audio_duration - aligned_duration)
                tolerance = max(aligned_duration, duration) * 0.05
                if drift > tolerance:
                    warnings.append(
                        f'Audio duration mismatch: audio is {audio_duration:.1f}s but the aligned chart is '
                        f'{aligned_duration:.1f}s. '
                        f'Check that the correct audio file was uploaded.'
                    )

        report('Extracting lyrics…', 72)
        if lyrics_entries is None and not is_gpif and gp_song is not None and song_meta is not None:
            try:
                lyrics_entries = lyrics_mod.extract_gp345_lyrics(
                    gp_song, track_indices, song_timeline.get('beats', []) if song_timeline else [],
                )
                if lyrics_entries:
                    warnings.append(
                        'Lyrics timing is approximate (GP3/4/5 only stores one lyric '
                        'line per measure, not per-note timing).'
                    )
            except Exception as e:
                warnings.append(f'Lyrics extraction failed: {e}')

        report('Detecting key signature…', 76)
        keys_data = None
        if song_timeline and song_timeline.get('beats'):
            try:
                if is_gpif:
                    keys_data = keys_mod.extract_gpif_keys(gpif_root, song_timeline['beats'])
                elif gp_song is not None:
                    keys_data = keys_mod.extract_gp345_keys(gp_song, song_timeline['beats'])
            except Exception as e:
                warnings.append(f'Key signature extraction failed: {e}')

        # 'existing_pack' mode carries its separated stems alongside (or
        # instead of) audio_path's 'full' mixdown — reused byte-for-byte
        # from the uploaded pack rather than collapsed into one mixdown.
        extra_stems_manifest: list[dict] | None = None
        extra_stem_paths: list[tuple[Path, str]] | None = None
        if has_existing_pack_stems:
            extra_stems_manifest = []
            extra_stem_paths = []
            for s in existing_pack['stems']:
                stem_src = Path(s['file'])
                dest_rel = f"stems/{s['id']}{stem_src.suffix.lower() or '.ogg'}"
                stem_entry = {'id': s['id'], 'file': dest_rel}
                if s.get('name'):
                    stem_entry['name'] = s['name']
                extra_stems_manifest.append(stem_entry)
                extra_stem_paths.append((stem_src, dest_rel))

        resolved_authors = authors or _extract_authors(gp_path)
        manifest = pack.assemble_manifest(
            title=use_title,
            artist=use_artist,
            album=use_album,
            year=year,
            album_artist=album_artist,
            track=track,
            disc=disc,
            genres=genres,
            mbid=mbid,
            isrc=isrc,
            language=language,
            authors=resolved_authors,
            duration=duration,
            arrangements=arrangement_entries,
            stem_file=(f'stems/full{Path(audio_path).suffix.lower()}' if audio_path else None),
            extra_stems=extra_stems_manifest,
            cover_file=(
                f'cover{Path(cover_path).suffix.lower() or ".jpg"}'
                if cover_path and Path(cover_path).exists() else None
            ),
            song_timeline_present=song_timeline is not None,
            lyrics_present=bool(lyrics_entries),
            keys_present=keys_data is not None,
            vocal_pitch_present=vocal_pitch_data is not None,
        )
        if audio_path is None and not extra_stems_manifest:
            warnings.append(
                'No audio stem — this pack is an authoring intermediate and '
                'will not validate until audio is added.'
            )

        report('Validating…', 85)
        validation = validate.validate_pack(
            manifest=manifest,
            arrangement_files=arrangement_files,
            song_timeline=song_timeline,
            keys=keys_data,
            vocal_pitch=vocal_pitch_data,
            drum_tab_files=drum_tab_files,
            notation_files=notation_files,
        )
        for part, errs in validation.items():
            warnings.append(f'{part}: {len(errs)} schema issue(s) — see validation report.')

        report('Assembling .feedpak…', 92)
        pak_bytes = pack.write_feedpak_zip(
            manifest=manifest,
            arrangement_files=arrangement_files,
            song_timeline=song_timeline,
            lyrics=lyrics_entries,
            keys=keys_data,
            vocal_pitch=vocal_pitch_data,
            drum_tab_files=drum_tab_files,
            notation_files=notation_files,
            audio_path=audio_path,
            extra_stem_paths=extra_stem_paths,
            cover_path=cover_path,
        )

        report('Done.', 100)
        return {
            'bytes': pak_bytes,
            'warnings': warnings,
            'validation': validation,
            'title': use_title,
            'artist': use_artist,
            'album': use_album,
            'duration': duration,
            'arrangement_count': len(arrangement_entries),
            'features': {
                'song_timeline': song_timeline is not None,
                'lyrics': bool(lyrics_entries),
                'keys': keys_data is not None,
                'vocal_pitch': vocal_pitch_data is not None,
                'drum_arrangements': len(drum_tab_files),
                'notation': len(notation_files),
                'handshapes': any(
                    arr.get('handshapes') for arr in arrangement_files.values()
                ),
                'tones': any(
                    arr.get('tones') for arr in arrangement_files.values()
                ),
                'real_audio': bool(
                    (audio_path and audio_mode in {'embedded', 'sync', 'existing_pack'})
                    or extra_stems_manifest
                ),
                # The pack already carries the source's separated stems —
                # offering "Split Stems" on top would try to re-split an
                # already-separated mix (or find nothing to split, when
                # there's no 'full' mixdown at all).
                'already_separated': bool(extra_stems_manifest),
            },
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
