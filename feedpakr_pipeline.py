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

_GP345_EXTS = {'.gp3', '.gp4', '.gp5'}
_GPX_EXTS = {'.gpx', '.gp'}  # GP6 / GP7-8, GPIF XML path

ALLOWED_ARRANGEMENT_NAMES = {'Lead', 'Rhythm', 'Bass', 'Drums', 'Keys', 'Vocals'}


class UnsupportedFormatError(Exception):
    """Raised for a file extension feedpakr does not handle."""


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
) -> tuple[str | None, float]:
    """Returns (audio_path, offset). audio_path is None on any failure —
    callers treat that as the §5.3.2 authoring-intermediate carve-out, not
    a hard error."""
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
    audio_mode: str = 'midi',
    user_audio_path: str | None = None,
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
    """
    _require_core()
    _check_extension(gp_path)

    if not track_indices:
        raise RuntimeError('No tracks selected.')

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
        )

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

        # Drums-as-arrangements (spec 1.17.0) needs gp2rs.convert_drum_track_to_drumtab,
        # which has no gp2rs_gpx equivalent — GPIF drum tracks stay on the
        # legacy fretted (string*24+fret) encoding gp2rs_gpx already wrote.
        drum_indices: set[int] = (
            set() if is_gpif else
            {t['index'] for t in parsed['tracks'] if t['is_drums'] and t['index'] in track_indices}
        )

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

            if idx is not None and idx in drum_indices:
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

            if is_gpif and idx is not None and track_by_index.get(idx, {}).get('is_piano'):
                try:
                    notation = notation_mod.convert_keys_track_notation(
                        gp_path, idx, track_by_index[idx]['name'],
                    )
                    if notation:
                        notation_filename = f'notation_{arr_id}.json'
                        notation_files[notation_filename] = notation
                        entry['notation'] = notation_filename
                except Exception as e:
                    warnings.append(f'Notation extraction failed for {arr.name}: {e}')

            arrangement_entries.append(entry)

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
                drift = abs(audio_duration - duration)
                tolerance = duration * 0.05
                if drift > tolerance:
                    warnings.append(
                        f'Audio duration mismatch: audio is {audio_duration:.1f}s but chart is {duration:.1f}s. '
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
                    gpif_root = gp2rs_gpx._load_gpif(gp_path)
                    keys_data = keys_mod.extract_gpif_keys(gpif_root, song_timeline['beats'])
                elif gp_song is not None:
                    keys_data = keys_mod.extract_gp345_keys(gp_song, song_timeline['beats'])
            except Exception as e:
                warnings.append(f'Key signature extraction failed: {e}')

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
            authors=authors,
            duration=duration,
            arrangements=arrangement_entries,
            stem_file=(f'stems/full{Path(audio_path).suffix.lower()}' if audio_path else None),
            cover_file=(
                f'cover{Path(cover_path).suffix.lower() or ".jpg"}'
                if cover_path and Path(cover_path).exists() else None
            ),
            song_timeline_present=song_timeline is not None,
            lyrics_present=bool(lyrics_entries),
            keys_present=keys_data is not None,
            vocal_pitch_present=vocal_pitch_data is not None,
        )
        if audio_path is None:
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
            },
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
