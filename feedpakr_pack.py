"""Manifest assembly and .feedpak zip writer.

Kept separate from the pipeline so it can be unit tested without needing
pyguitarpro or a real GP file — it only deals in plain dicts.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import yaml

# feedpakr always stamps the spec version it targets (feedpak spec §4:
# Readers accept any minor version of the same major they support), not
# the host's own FEEDPAK_VERSION constant — the host writer being behind
# the spec doesn't mean feedpakr's output should be.
FEEDPAK_VERSION = "1.19.0"

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\s]+')


def sanitize_filename_component(text: str, max_len: int = 60) -> str:
    """Collapse unsafe/whitespace characters into '_' for use in a filename."""
    cleaned = _UNSAFE_CHARS.sub('_', (text or '').strip()).strip('_')
    return cleaned[:max_len] or 'untitled'


def unique_output_path(out_dir: str | Path, base_name: str, ext: str = '.feedpak') -> Path:
    """Return a non-colliding path under out_dir, appending _2, _3, … as needed."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = out_dir / f'{base_name}{ext}'
    n = 2
    while candidate.exists():
        candidate = out_dir / f'{base_name}_{n}{ext}'
        n += 1
    return candidate


def arrangement_id_for(name: str, taken: set[str]) -> str:
    """Derive a stable, unique arrangement id from a display name.

    Mirrors the de-duplication scheme used elsewhere in the ecosystem
    (lead, lead2, lead3, …) so ids stay predictable across tools.
    """
    base = re.sub(r'[^a-z0-9]', '', (name or 'arr').lower()) or 'arr'
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f'{base}{n}'
        n += 1
    taken.add(candidate)
    return candidate


def assemble_manifest(
    *,
    title: str,
    artist: str,
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
    duration: float,
    arrangements: list[dict],
    stem_file: str | None,
    extra_stems: list[dict] | None = None,
    cover_file: str | None = None,
    song_timeline_present: bool = False,
    lyrics_present: bool = False,
    keys_present: bool = False,
    vocal_pitch_present: bool = False,
) -> dict:
    """Build the manifest.yaml dict. Only emits keys feedpakr actually fills.

    stem_file is the manifest-relative path of the packed full mixdown
    (e.g. "stems/full.ogg" or "stems/full.wav"), or None when no audio
    could be produced. extra_stems is an optional list of additional stem
    entries (`{'id', 'file', 'name'?}`, file already manifest-relative) —
    used by the 'existing_pack' audio mode, which keeps a source pack's
    original separated stems instead of collapsing everything to one
    mixdown. cover_file is the manifest-relative path of the packed cover
    image (e.g. "cover.jpg"), or None when there isn't one —
    per spec §2.2 ("nothing is auto-discovered by scanning; a Reader MUST
    NOT rely on filename"), writing cover.jpg into the zip without also
    pointing the manifest's `cover` key at it means a compliant Reader
    never surfaces it, even though the bytes are right there.

    album_artist/track/disc/genres/mbid/isrc/language/authors are all
    optional, schema-valid top-level manifest keys (spec §5.1/§5.4) —
    editable in the editor plugin's create-modal but, until now, never
    exposed anywhere in feedpakr's own import flow. `authors` here is a
    flat list of names (the UI's "who charted this" field); each becomes
    an `{name}` object per spec §5.4 — role/email/url aren't collected
    here, so they're simply omitted rather than sent empty.
    """
    manifest: dict = {
        'feedpak_version': FEEDPAK_VERSION,
        'title': title or 'Unknown',
        'artist': artist or 'Unknown',
    }
    if album:
        manifest['album'] = album
    if year:
        manifest['year'] = int(year)
    if album_artist:
        manifest['album_artist'] = album_artist
    if track:
        manifest['track'] = int(track)
    if disc:
        manifest['disc'] = int(disc)
    if genres:
        manifest['genres'] = list(genres)
    if mbid:
        manifest['mbid'] = mbid
    if isrc:
        manifest['isrc'] = isrc
    if language:
        manifest['language'] = language
    normalized_authors = [name.strip() for name in (authors or []) if name.strip()]
    if normalized_authors:
        manifest['authors'] = [{'name': name} for name in normalized_authors]
    manifest['duration'] = float(duration)
    manifest['arrangements'] = arrangements

    # `stems` is schema-required with minItems 1. When no audio could be
    # produced the pack is a local authoring intermediate (feedpak spec
    # §5.3.2 carve-out, same precedent as the musicxml-import plugin) —
    # still written so the caller can inspect/fix it, but it will not pass
    # strict validation until audio is added. That is surfaced as a
    # warning by the pipeline, not hidden here.
    stems_list: list[dict] = (
        [{'id': 'full', 'file': stem_file, 'default': True}]
        if stem_file else []
    )
    for s in (extra_stems or []):
        entry = {'id': s['id'], 'file': s['file']}
        if s.get('name'):
            entry['name'] = s['name']
        stems_list.append(entry)
    manifest['stems'] = stems_list
    if cover_file:
        manifest['cover'] = cover_file

    if song_timeline_present:
        manifest['song_timeline'] = 'song_timeline.json'
    if lyrics_present:
        manifest['lyrics'] = 'lyrics.json'
        manifest['lyrics_source'] = 'authored'
    if keys_present:
        manifest['keys'] = 'keys.json'
    if vocal_pitch_present:
        manifest['vocal_pitch'] = 'vocal_pitch.json'

    return manifest


def write_feedpak_zip(
    *,
    manifest: dict,
    arrangement_files: dict[str, dict],
    song_timeline: dict | None = None,
    lyrics: list | None = None,
    keys: dict | None = None,
    vocal_pitch: dict | None = None,
    drum_tab_files: dict[str, dict] | None = None,
    notation_files: dict[str, dict] | None = None,
    audio_path: str | Path | None = None,
    extra_stem_paths: list[tuple[str | Path, str]] | None = None,
    cover_path: str | Path | None = None,
) -> bytes:
    """Assemble a .feedpak zip in memory and return its bytes.

    arrangement_files maps the manifest-relative filename (e.g.
    "lead.json") to its wire-format payload dict. drum_tab_files and
    notation_files are the same shape but written at the package root
    (spec §7 side files, not under arrangements/) — their filenames must
    already match what the corresponding arrangement entry's `drum_tab`
    / `notation` pointer says.

    extra_stem_paths is an optional list of (source_path, dest_rel) pairs
    copied into the zip verbatim (dest_rel e.g. "stems/guitar.ogg") —
    written alongside audio_path's "full" mixdown, keeping the
    manifest-relative names assemble_manifest's extra_stems used.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            'manifest.yaml',
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        )
        for filename, payload in arrangement_files.items():
            zf.writestr(
                f'arrangements/{filename}',
                json.dumps(payload, separators=(',', ':')),
            )
        for filename, payload in (drum_tab_files or {}).items():
            zf.writestr(filename, json.dumps(payload, separators=(',', ':')))
        for filename, payload in (notation_files or {}).items():
            zf.writestr(filename, json.dumps(payload, separators=(',', ':')))
        if song_timeline is not None:
            zf.writestr(
                'song_timeline.json',
                json.dumps(song_timeline, separators=(',', ':')),
            )
        if lyrics is not None:
            zf.writestr('lyrics.json', json.dumps(lyrics, separators=(',', ':')))
        if keys is not None:
            zf.writestr('keys.json', json.dumps(keys, separators=(',', ':')))
        if vocal_pitch is not None:
            zf.writestr('vocal_pitch.json', json.dumps(vocal_pitch, separators=(',', ':')))
        if audio_path and Path(audio_path).exists():
            stem_ext = Path(audio_path).suffix.lower() or '.ogg'
            zf.write(audio_path, f'stems/full{stem_ext}')
        for src, dest_rel in (extra_stem_paths or []):
            if src and Path(src).exists():
                zf.write(src, dest_rel)
        if cover_path and Path(cover_path).exists():
            ext = Path(cover_path).suffix.lower() or '.jpg'
            zf.write(cover_path, f'cover{ext}')
    return buf.getvalue()
