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
    / _read_manifest_from_zip closely enough for this purpose."""
    if src.is_dir():
        for name in ('manifest.yaml', 'manifest.yml'):
            mf = src / name
            if mf.exists():
                data = yaml.safe_load(mf.read_text(encoding='utf-8'))
                return data if isinstance(data, dict) else {}
        raise FileNotFoundError(f'manifest.yaml not found in {src}')
    with zipfile.ZipFile(src) as zf:
        for name in ('manifest.yaml', 'manifest.yml'):
            try:
                data = yaml.safe_load(zf.read(name).decode('utf-8'))
                if isinstance(data, dict):
                    return data
            except KeyError:
                continue
    raise FileNotFoundError(f'manifest.yaml not found in {src}')


def _list_members(src: Path) -> list[str]:
    if src.is_dir():
        return [
            p.relative_to(src).as_posix()
            for p in src.rglob('*') if p.is_file()
        ]
    with zipfile.ZipFile(src) as zf:
        return [n for n in zf.namelist() if not n.endswith('/')]


def _read_member(src: Path, rel: str) -> bytes | None:
    if sloppak_mod is not None:
        return sloppak_mod.read_member_bytes(src, rel)
    if src.is_dir():
        target = (src / rel).resolve()
        try:
            target.relative_to(src.resolve())
        except ValueError:
            return None
        return target.read_bytes() if target.is_file() else None
    with zipfile.ZipFile(src) as zf:
        try:
            return zf.read(rel)
        except KeyError:
            return None


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

    members = _list_members(src)
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
        'features': {'real_audio': real_audio},
    }
