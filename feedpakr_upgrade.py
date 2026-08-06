"""sloppak -> feedpak upgrade path.

Migrates an existing `.sloppak` to `.feedpak`: stamps the target
`feedpak_version`, promotes `song_timeline.json` from the first
arrangement carrying embedded `beats`/`sections` (the convention every
sample pack in this project's fixtures actually uses — feedpakr's own
GP pipeline writes a proper song-level `song_timeline.json` instead, but
older/other-tool packs embed beats/sections per-arrangement), and
ensures `stems[]` has an `id: full` entry, promoting the deprecated
`original_audio` key when one is present.

Every other file is copied through byte-for-byte and every other
manifest key is preserved untouched (spec §9 — an upgrade pass only
ADDS data, never removes or reinterprets what's already there). The
original `.sloppak` is never modified or deleted.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import yaml

try:
    import sloppak as sloppak_mod
except ImportError:  # pragma: no cover
    sloppak_mod = None

import feedpakr_pack as pack
import feedpakr_validate as validate

FEEDPAK_VERSION = pack.FEEDPAK_VERSION


def _load_manifest_fallback(src: Path) -> dict:
    """Minimal manifest read for environments without the host's sloppak
    module (e.g. a bare test run) — mirrors sloppak.py's own _read_manifest
    / _read_manifest_from_zip closely enough for this purpose.

    Both branches are bounded by _MAX_MEMBER_BYTES: `src` is untrusted
    (attacker-supplied) content, and manifest.yaml is the very first thing
    read off it, before any other size check in the upload path has run.
    """
    if src.is_dir():
        for name in ('manifest.yaml', 'manifest.yml'):
            mf = src / name
            if not mf.exists():
                continue
            try:
                if mf.stat().st_size > _MAX_MEMBER_BYTES:
                    continue
            except OSError:
                continue
            data = yaml.safe_load(mf.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        raise FileNotFoundError(f'manifest.yaml not found in {src}')
    with zipfile.ZipFile(src) as zf:
        for name in ('manifest.yaml', 'manifest.yml'):
            raw = _read_zip_member_capped(zf, name)
            if raw is None:
                continue
            data = yaml.safe_load(raw.decode('utf-8'))
            if isinstance(data, dict):
                return data
    raise FileNotFoundError(f'manifest.yaml not found in {src}')


def _is_safe_archive_member(name: str) -> bool:
    """True if `name` is safe to use as a member path inside a zip we are
    building (or as a relative-to-src filesystem path we'll open).

    Source `.sloppak`/`.feedpak` files are untrusted content — nothing
    stops a crafted archive from declaring a member named e.g.
    ``../../../etc/cron.d/evil`` or an absolute path. `_list_members` /
    `zipfile.namelist()` return such names verbatim; copying them straight
    into a newly-built zip (`zf.writestr(rel, raw)`) would silently forward
    the poisoned path into the produced .feedpak, where it becomes a
    zip-slip payload for whatever later extracts that file (a naive
    `extractall()`, a different tool, a future host version). Reject
    anything that isn't a plain, relative, forward-slash path.
    """
    if not name or name.startswith('/') or name.startswith('\\'):
        return False
    # Reject drive-letter / UNC-style absolute paths on Windows hosts too.
    if len(name) >= 2 and name[1] == ':':
        return False
    posix = name.replace('\\', '/')
    parts = posix.split('/')
    if any(part in ('', '..') for part in parts):
        return False
    return True


def _list_members(src: Path) -> tuple[list[str], int]:
    """Return (safe_member_names, unsafe_skipped_count)."""
    if src.is_dir():
        # Real filesystem walks under src can't produce a '..'-escaping
        # relative path, so every entry here is trivially safe.
        names = [p.relative_to(src).as_posix() for p in src.rglob('*') if p.is_file()]
        return names, 0
    with zipfile.ZipFile(src) as zf:
        raw = [n for n in zf.namelist() if not n.endswith('/')]
    safe = [n for n in raw if _is_safe_archive_member(n)]
    return safe, len(raw) - len(safe)


# A single zip member is untrusted, attacker-controlled content — nothing
# bounds how much it inflates to on decompression ("zip bomb"). zipfile's
# own .read() buffers the entire decompressed stream in memory before
# returning, so a small crafted upload can exhaust host memory well before
# any size check on the *compressed* upload (routes.py's _MAX_*_BYTES) ever
# has a chance to reject it. Read in bounded chunks and abort early instead.
_MAX_MEMBER_BYTES = 256 * 1024 * 1024  # 256 MB — generous for one stem/cover/arrangement file


def _read_zip_member_capped(zf: zipfile.ZipFile, rel: str) -> bytes | None:
    try:
        info = zf.getinfo(rel)
    except KeyError:
        return None
    chunks: list[bytes] = []
    total = 0
    with zf.open(info) as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_MEMBER_BYTES:
                return None
            chunks.append(chunk)
    return b''.join(chunks)


def _read_member(src: Path, rel: str) -> bytes | None:
    if not _is_safe_archive_member(rel):
        return None
    if sloppak_mod is not None:
        return sloppak_mod.read_member_bytes(src, rel)
    if src.is_dir():
        target = (src / rel).resolve()
        try:
            target.relative_to(src.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            if target.stat().st_size > _MAX_MEMBER_BYTES:
                return None
        except OSError:
            return None
        return target.read_bytes()
    with zipfile.ZipFile(src) as zf:
        return _read_zip_member_capped(zf, rel)


def _section_time(entry: dict) -> float | None:
    """Sections have been seen in the wild keyed by both `time` (the
    convention this project's own gp2rs/song.py pipeline and every real
    sample fixture use) and `start_time` (the editor plugin's save
    format) — accept either rather than silently dropping one variant's
    packs."""
    for key in ('time', 'start_time'):
        if key in entry:
            try:
                return float(entry[key])
            except (TypeError, ValueError):
                continue
    return None


def _build_song_timeline(manifest: dict, src: Path) -> dict | None:
    """Promote the first arrangement's embedded beats/sections into a
    proper song_timeline.json side file. None if no arrangement has any."""
    for arr in manifest.get('arrangements', []) or []:
        rel = arr.get('file')
        if not rel:
            continue
        raw = _read_member(src, rel)
        if raw is None:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue

        beats = payload.get('beats') or []
        sections = payload.get('sections') or []
        if not beats and not sections:
            continue

        timeline: dict = {'version': 1}
        if beats:
            timeline['beats'] = [
                {'time': b.get('time', 0.0), 'measure': b.get('measure', -1)}
                for b in beats
            ]
        if sections:
            entries = []
            for s in sections:
                t = _section_time(s)
                if t is None:
                    continue
                entries.append({'name': s.get('name', ''), 'number': s.get('number', 0), 'time': t})
            if entries:
                timeline['sections'] = entries

        return timeline if (timeline.get('beats') or timeline.get('sections')) else None
    return None


def _has_phrase_ladder(manifest: dict, src: Path) -> bool:
    """True when arrangement 0 already carries phrase-level difficulty."""
    arrangements = manifest.get('arrangements', []) or []
    if not arrangements:
        return False
    rel = arrangements[0].get('file')
    if not rel:
        return False
    raw = _read_member(src, rel)
    if raw is None:
        return False
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if payload.get('phrases'):
        return True
    return False


_VALID_LYRICS_SOURCES = {'authored', 'transcribed', 'user'}

# Known non-spec values written by this ecosystem's own now-superseded
# tools, mapped to the correct enum value: the legacy GP-import pipeline
# wrote 'xml' for chart-authored lyrics, and CDLC-derived packs wrote
# 'sng' (Rocksmith's proprietary .sng chart format) for the same thing —
# both match spec's own "authored (from an authored chart)" definition.
_KNOWN_LEGACY_LYRICS_SOURCES = {'xml': 'authored', 'sng': 'authored'}


def _normalize_lyrics_source(manifest: dict, warnings: list[str]) -> None:
    """Some pre-spec packs put a non-vocabulary value into `lyrics_source`
    — either a known legacy tool convention ('xml') or a transcription
    engine name directly (e.g. 'whisperx', usually alongside a correctly-
    shaped `lyric_transcription` block that already names the engine
    properly). Both are safe, non-lossy fixes: nothing is discarded, just
    relabeled to match the spec's fixed vocabulary. Anything else is left
    alone and warned about, rather than guessed at blindly."""
    source = manifest.get('lyrics_source')
    if not isinstance(source, str) or source in _VALID_LYRICS_SOURCES:
        return

    if source in _KNOWN_LEGACY_LYRICS_SOURCES:
        corrected = _KNOWN_LEGACY_LYRICS_SOURCES[source]
        manifest['lyrics_source'] = corrected
        warnings.append(
            f"lyrics_source: {source!r} is a known legacy value from this ecosystem's "
            f"own earlier tooling — corrected to {corrected!r}."
        )
    elif isinstance(manifest.get('lyric_transcription'), dict):
        manifest['lyrics_source'] = 'transcribed'
        warnings.append(
            f"lyrics_source: {source!r} is not valid feedpak vocabulary — corrected to "
            f"'transcribed' (the engine name is already recorded correctly in lyric_transcription)."
        )
    else:
        warnings.append(
            f"lyrics_source: {source!r} is not valid feedpak vocabulary "
            f"(must be authored/transcribed/user) — left as-is, no lyric_transcription "
            f"block to infer the correct value from."
        )


def _promote_full_stem(manifest: dict, src: Path, warnings: list[str]) -> None:
    stems = manifest.get('stems') or []
    if any(str(s.get('id')) == 'full' for s in stems):
        return

    # A pack with exactly one stem, not named 'full', is a single unseparated
    # mix that just used a different id (e.g. the tutorial packs' 'audio') —
    # that stem's file IS the complete mixdown, so relabeling its id is a
    # safe, non-lossy fix. With 2+ stems this would be wrong (those are
    # genuinely separated instrument stems, none of which is a full mix on
    # its own), so this only applies to the single-stem case.
    if len(stems) == 1:
        original_id = stems[0].get('id', '?')
        stems[0]['id'] = 'full'
        warnings.append(
            f"Renamed the pack's only stem (id {original_id!r} originally) "
            f"to the reserved 'full' id — a single unseparated stem is the complete mix."
        )
        return

    legacy_rel = manifest.get('original_audio')
    if isinstance(legacy_rel, str) and legacy_rel.strip():
        rel = legacy_rel.strip()
        if _read_member(src, rel) is not None:
            manifest['stems'] = stems + [{'id': 'full', 'file': rel, 'default': False}]
            manifest.pop('original_audio', None)
            warnings.append(
                f"Promoted the deprecated 'original_audio' key to a proper "
                f"'full' stem entry ({rel})."
            )
            return
        warnings.append("'original_audio' key present but the file could not be read — left as-is.")
        return

    warnings.append(
        "No 'full' mixdown stem, and no legacy 'original_audio' to promote — "
        "this pack has no complete mix (only separated stems, or none at all)."
    )


def upgrade_sloppak(sloppak_path: str) -> dict:
    """Convert one .sloppak (dir or zip) to .feedpak bytes. Returns:

        {'bytes': ..., 'warnings': [...], 'validation': {...},
         'title': str, 'artist': str}

    Raises only if the source can't be read as a sloppak at all (no
    manifest) — every other failure degrades into `warnings`.
    """
    src = Path(sloppak_path)
    manifest = (
        sloppak_mod.load_manifest(src) if sloppak_mod is not None
        else _load_manifest_fallback(src)
    )
    warnings: list[str] = []

    new_manifest = dict(manifest)
    new_manifest['feedpak_version'] = FEEDPAK_VERSION
    has_recorded_audio_provenance = isinstance(new_manifest.get('stem_separation'), dict)

    _promote_full_stem(new_manifest, src, warnings)
    _normalize_lyrics_source(new_manifest, warnings)

    members, unsafe_count = _list_members(src)
    if unsafe_count:
        warnings.append(
            f'{unsafe_count} file(s) in the source pack had unsafe/escaping archive '
            'paths and were omitted from the upgrade.'
        )
    manifest_filenames = {'manifest.yaml', 'manifest.yml'}
    copy_members = [m for m in members if Path(m).name not in manifest_filenames]

    song_timeline = None
    if 'song_timeline' not in new_manifest and 'song_timeline.json' not in copy_members:
        try:
            song_timeline = _build_song_timeline(new_manifest, src)
        except Exception:
            song_timeline = None
        if song_timeline:
            new_manifest['song_timeline'] = 'song_timeline.json'
            warnings.append('Promoted arrangement-embedded beats/sections into song_timeline.json.')

    validation = validate.validate_pack(
        manifest=new_manifest,
        arrangement_files={},  # embedded arrangement JSON is copied verbatim, unchanged — not re-validated
        song_timeline=song_timeline,
    )
    for part, errs in validation.items():
        warnings.append(f'{part}: {len(errs)} schema issue(s) — see validation report.')

    buf = io.BytesIO()
    written_members: set[str] = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            'manifest.yaml',
            yaml.safe_dump(new_manifest, allow_unicode=True, sort_keys=False),
        )
        if song_timeline is not None:
            zf.writestr('song_timeline.json', json.dumps(song_timeline, separators=(',', ':')))
        for rel in copy_members:
            raw = _read_member(src, rel)
            if raw is None:
                warnings.append(f'Could not read {rel!r} from the source pack — omitted from the upgrade.')
                continue
            zf.writestr(rel, raw)
            written_members.add(rel)

    phrase_ladder = _has_phrase_ladder(new_manifest, src)
    real_audio = any(
        has_recorded_audio_provenance
        and str(stem.get('id')) == 'full'
        and isinstance(stem.get('file'), str)
        and stem['file'] in written_members
        for stem in (new_manifest.get('stems') or [])
        if isinstance(stem, dict)
    )

    return {
        'bytes': buf.getvalue(),
        'warnings': warnings,
        'validation': validation,
        'title': new_manifest.get('title', ''),
        'artist': new_manifest.get('artist', ''),
        'features': {'real_audio': real_audio, 'phrase_ladder': phrase_ladder},
    }


# ── Existing-pack audio reuse (feedpakr's 'existing_pack' audio mode) ──────
#
# Lets a GP re-import keep a pack's original audio/stems/cover instead of
# synthesizing/embedding/re-recording — "upload a sloppak/feedpak, then
# re-import the GP file on top of it": the chart (arrangements, timeline,
# tones, capo, notation, keys) is fully replaced by the fresh GP parse, but
# the audio side is reused byte-for-byte from the uploaded pack, with the
# new chart's timing re-aligned to it via the same chroma-DTW autosync used
# for a user-supplied recording.

def extract_pack_assets(pack_path: str | Path, out_dir: str | Path) -> dict:
    """Extract every stem, the reserved 'full' mixdown (if present), and the
    cover image from an existing .sloppak/.feedpak into out_dir.

    Returns:
        {'stems': [{'id', 'file' (absolute path), 'name'?}, ...],
         'full_mix_path': str | None,
         'cover_path': str | None,
         'sync_reference_path': str | None,
         'error': str | None}

    sync_reference_path is full_mix_path when the pack has one; otherwise
    it's an ffmpeg-mixed-down reference built from the separated stems,
    used ONLY to autosync the freshly-imported chart — it is never itself
    written into the output pack. On any failure this returns a dict with
    a non-empty 'error' rather than raising, matching every other
    best-effort function in this plugin.
    """
    src = Path(pack_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        manifest = (
            sloppak_mod.load_manifest(src) if sloppak_mod is not None
            else _load_manifest_fallback(src)
        )
    except Exception as e:
        return {'error': f'Could not read manifest: {e}'}

    raw_stems = manifest.get('stems')
    if not isinstance(raw_stems, list) or not raw_stems:
        return {'error': 'This pack has no stems to reuse.'}

    full_mix_path: str | None = None
    stems: list[dict] = []
    used_names: set[str] = set()
    warnings: list[str] = []
    for s in raw_stems:
        if not isinstance(s, dict):
            continue
        raw_sid = str(s.get('id', ''))
        sid = pack.sanitize_stem_id_component(raw_sid)
        sfile = str(s.get('file', ''))
        if not raw_sid or not sfile:
            continue
        data = _read_member(src, sfile)
        if data is None:
            warnings.append(f"Declared stem '{sid}' could not be read and was skipped.")
            continue
        dest_name = Path(sfile).name
        # Two stems sharing a basename (different source subdirs) would
        # otherwise silently overwrite each other on disk.
        if dest_name in used_names:
            dest_name = f'{sid}_{dest_name}'
        used_names.add(dest_name)
        dest = out / dest_name
        dest.write_bytes(data)
        if sid == 'full':
            full_mix_path = str(dest)
        else:
            entry = {'id': sid, 'file': str(dest)}
            if isinstance(s.get('name'), str) and s['name'].strip():
                entry['name'] = s['name'].strip()
            stems.append(entry)

    if full_mix_path is None and not stems:
        return {'error': 'This pack has no stems to reuse.', 'warnings': warnings}

    cover_path: str | None = None
    cover_rel = manifest.get('cover')
    if isinstance(cover_rel, str) and cover_rel:
        cdata = _read_member(src, cover_rel)
        if cdata is not None:
            cover_name = Path(cover_rel).name
            if cover_name in used_names:
                cover_name = f"{pack.sanitize_stem_id_component(Path(cover_name).stem, fallback='cover')}{Path(cover_name).suffix}"
                n = 2
                while cover_name in used_names:
                    cover_name = f"{pack.sanitize_stem_id_component(Path(cover_rel).stem, fallback='cover')}_{n}{Path(cover_rel).suffix}"
                    n += 1
            used_names.add(cover_name)
            cdest = out / cover_name
            cdest.write_bytes(cdata)
            cover_path = str(cdest)

    sync_reference_path = full_mix_path
    if sync_reference_path is None:
        mix_out = out / 'sync_reference.ogg'
        err = _mixdown_stems([s['file'] for s in stems], str(mix_out))
        if err:
            return {'error': f'Could not prepare audio for syncing: {err}'}
        sync_reference_path = str(mix_out)

    return {
        'stems': stems,
        'full_mix_path': full_mix_path,
        'cover_path': cover_path,
        'sync_reference_path': sync_reference_path,
        'warnings': warnings,
        'error': None,
    }


def _mixdown_stems(stem_paths: list[str], out_path: str, timeout: int = 120) -> str | None:
    """ffmpeg amix of separated stems into one reference track, used only to
    autosync a freshly-imported GP chart against a pack that has no 'full'
    mixdown of its own. Returns an error string, or None on success."""
    import shutil
    import subprocess

    if not stem_paths:
        return 'No stems to mix.'
    if len(stem_paths) == 1:
        try:
            shutil.copy2(stem_paths[0], out_path)
            return None
        except OSError as e:
            return str(e)

    cmd = ['ffmpeg', '-y']
    for p in stem_paths:
        cmd += ['-i', p]
    cmd += [
        '-filter_complex', f'amix=inputs={len(stem_paths)}:duration=longest:dropout_transition=0',
        '-q:a', '6', out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 'ffmpeg not found — cannot mix stems for syncing.'
    except subprocess.TimeoutExpired:
        return 'ffmpeg mixdown timed out.'
    if result.returncode != 0 or not Path(out_path).exists():
        return f'ffmpeg mixdown failed: {result.stderr[-300:].decode(errors="replace")}'
    return None
