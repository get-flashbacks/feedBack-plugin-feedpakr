"""GP3-GP5 -> .feedpak import pipeline.

Phase 1 scope: GP3/GP4/GP5 only (the pyguitarpro path). GP6 (.gpx) and
GP7/GP8 (.gp) route through gp2rs_gpx in the host core and are wired up
in a later phase — see the plan's phased milestones. Calling parse_gp /
build_feedpak on a .gpx/.gp file raises UnsupportedFormatError with a
message saying so, rather than failing confusingly deep in gp2rs.

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
    import gp2midi
except ImportError:  # pragma: no cover
    gp2midi = None

try:
    import song as song_mod
except ImportError:  # pragma: no cover
    song_mod = None

import feedpakr_pack as pack
import feedpakr_validate as validate

log = logging.getLogger('feedBack.plugin.feedpakr')

_GP345_EXTS = {'.gp3', '.gp4', '.gp5'}
_GPX_EXTS = {'.gpx', '.gp'}  # GP6 / GP7-8 - not yet supported (phase 2)

ALLOWED_ARRANGEMENT_NAMES = {'Lead', 'Rhythm', 'Bass', 'Drums', 'Keys', 'Vocals'}


class UnsupportedFormatError(Exception):
    """Raised for a file extension feedpakr does not (yet) handle."""


def _require_core() -> None:
    if guitarpro is None or gp2rs is None or song_mod is None:
        raise RuntimeError(
            'feedpakr requires the feedBack host core lib (guitarpro, gp2rs, '
            'song) on sys.path — it must run inside the feedBack app.'
        )


def _check_extension(gp_path: str) -> None:
    ext = Path(gp_path).suffix.lower()
    if ext in _GPX_EXTS:
        raise UnsupportedFormatError(
            f'{ext} (GP6/GP7/GP8) support is coming in a later phase — only '
            '.gp3/.gp4/.gp5 files are supported today.'
        )
    if ext not in _GP345_EXTS:
        raise UnsupportedFormatError(f'Unsupported file extension: {ext}')


def parse_gp(gp_path: str) -> dict:
    """Parse a GP3/4/5 file and return metadata + track list for the upload UI."""
    _require_core()
    _check_extension(gp_path)

    gp_song = guitarpro.parse(gp_path)
    tracks = gp2rs.list_tracks(gp_path)
    _, auto_names = gp2rs.auto_select_tracks(gp_path)
    for t in tracks:
        auto_role = auto_names.get(t['index'])
        t['auto_name'] = auto_role or ''
        t['auto_selected'] = auto_role is not None

    return {
        'title': gp_song.title or Path(gp_path).stem,
        'artist': gp_song.artist or '',
        'album': gp_song.album or '',
        'tempo': gp_song.tempo,
        'tracks': tracks,
    }


def _capo_for_track(gp_song, track_index: int) -> int:
    """GP's per-track capo fret. pyguitarpro exposes it as Track.offset —
    gp2rs never reads it (it hardcodes <capo>0</capo> in the RS XML), so
    feedpakr overrides the wire value with the real one post-conversion."""
    try:
        return max(0, int(gp_song.tracks[track_index].offset or 0))
    except (IndexError, AttributeError, TypeError, ValueError):
        return 0


def _load_song_meta(xml_dir: Path):
    """Load song-level data (beats/sections/duration) from the converted RS
    XML via the same song.load_song() the rest of the host uses — these
    times are playback-schedule-accurate (repeat/D.S./D.C. expansion
    already applied by gp2rs), unlike a naive per-measure tempo walk.
    Returns None on failure (caller degrades gracefully)."""
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


def build_feedpak(
    gp_path: str,
    *,
    track_indices: list[int],
    arrangement_names: dict[int, str],
    title: str = '',
    artist: str = '',
    album: str = '',
    want_audio: bool = True,
    cover_path: str | None = None,
    report=lambda stage, pct: None,
) -> dict:
    """Run the full GP3-5 -> .feedpak pipeline. Returns:

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

    warnings: list[str] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix='feedpakr_build_'))
    try:
        report('Parsing Guitar Pro file…', 5)
        gp_song = guitarpro.parse(gp_path)
        use_title = (title or '').strip() or gp_song.title or Path(gp_path).stem
        use_artist = (artist or '').strip() or gp_song.artist or ''
        use_album = (album or '').strip() or gp_song.album or ''

        names = {
            idx: arrangement_names.get(idx, '')
            for idx in track_indices
        }

        report('Converting tracks to arrangement XML…', 20)
        xml_dir = tmp_dir / 'xml'
        xml_paths = gp2rs.convert_file(
            gp_path, str(xml_dir),
            track_indices=track_indices,
            arrangement_names=names,
        )
        if not xml_paths:
            raise RuntimeError('No arrangements produced from the selected tracks.')

        report('Generating audio…', 40)
        audio_path: str | None = None
        if want_audio:
            if gp2midi is None:
                warnings.append('Audio skipped: gp2midi not available on this host.')
            else:
                try:
                    audio_base = str(tmp_dir / 'audio')
                    audio_path = gp2midi.gp_to_audio(gp_path, audio_base, track_indices)
                except Exception as e:
                    warnings.append(f'Audio skipped: {e}')
        else:
            report('Audio skipped (unchecked).', 45)

        report('Reading arrangement data…', 55)
        arrangement_entries: list[dict] = []
        arrangement_files: dict[str, dict] = {}
        taken_ids: set[str] = set()

        # Map each output XML back to the GP track it came from, so the
        # capo fix-up (below) reads the right track's offset. convert_file
        # names files "<track-name>_<arr-name>.xml" in the same order as
        # track_indices, one file per selected track.
        for idx, xml_path in zip(track_indices, xml_paths):
            try:
                arr = song_mod.parse_arrangement(xml_path)
            except Exception as e:
                warnings.append(f'Skipped an arrangement — failed to read {Path(xml_path).name}: {e}')
                continue

            # capo: gp2rs always writes 0 (lib/gp2rs.py never reads GP's
            # per-track offset) — override with the real value here.
            arr.capo = _capo_for_track(gp_song, idx)

            wire = song_mod.arrangement_to_wire(arr)
            arr_id = pack.arrangement_id_for(arr.name, taken_ids)
            filename = f'{arr_id}.json'
            arrangement_files[filename] = wire
            arrangement_entries.append({
                'id': arr_id,
                'name': arr.name,
                'file': f'arrangements/{filename}',
                'tuning': list(arr.tuning),
                'capo': arr.capo,
            })

        if not arrangement_entries:
            raise RuntimeError('None of the selected tracks could be converted.')

        report('Building timeline (sections, beats)…', 70)
        song_meta = _load_song_meta(xml_dir)
        song_timeline = _song_timeline_from_meta(song_meta)
        if song_timeline is None:
            warnings.append('No sections/beats found in the source file.')
        duration = float(song_meta.song_length) if song_meta else 0.0

        manifest = pack.assemble_manifest(
            title=use_title,
            artist=use_artist,
            album=use_album,
            duration=duration,
            arrangements=arrangement_entries,
            stem_file=(f'stems/full{Path(audio_path).suffix.lower()}' if audio_path else None),
            song_timeline_present=song_timeline is not None,
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
        )
        for part, errs in validation.items():
            warnings.append(f'{part}: {len(errs)} schema issue(s) — see validation report.')

        report('Assembling .feedpak…', 92)
        pak_bytes = pack.write_feedpak_zip(
            manifest=manifest,
            arrangement_files=arrangement_files,
            song_timeline=song_timeline,
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
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
