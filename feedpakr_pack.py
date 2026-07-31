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
    authors: list[str] | None = None,
    year: int | None = None,
    duration: float,
    arrangements: list[dict],
    stem_file: str | None,
    song_timeline_present: bool = False,
    lyrics_present: bool = False,
    keys_present: bool = False,
    vocal_pitch_present: bool = False,
) -> dict:
    """Build the manifest.yaml dict. Only emits keys feedpakr actually fills.

    stem_file is the manifest-relative path of the packed full mixdown
    (e.g. "stems/full.ogg" or "stems/full.wav"), or None when no audio
    could be produced.
    """
    manifest: dict = {
        'feedpak_version': FEEDPAK_VERSION,
        'title': title or 'Unknown',
        'artist': artist or 'Unknown',
    }
    if album:
        manifest['album'] = album
    if authors:
        manifest['authors'] = authors
    if year:
        manifest['year'] = int(year)
    manifest['duration'] = float(duration)
    manifest['arrangements'] = arrangements

    # `stems` is schema-required with minItems 1. When no audio could be
    # produced the pack is a local authoring intermediate (feedpak spec
    # §5.3.2 carve-out, same precedent as the musicxml-import plugin) —
    # still written so the caller can inspect/fix it, but it will not pass
    # strict validation until audio is added. That is surfaced as a
    # warning by the pipeline, not hidden here.
    manifest['stems'] = (
        [{'id': 'full', 'file': stem_file, 'default': True}]
        if stem_file else []
    )

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
    cover_path: str | Path | None = None,
) -> bytes:
    """Assemble a .feedpak zip in memory and return its bytes.

    arrangement_files maps the manifest-relative filename (e.g.
    "lead.json") to its wire-format payload dict. drum_tab_files and
    notation_files are the same shape but written at the package root
    (spec §7 side files, not under arrangements/) — their filenames must
    already match what the corresponding arrangement entry's `drum_tab`
    / `notation` pointer says.
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
        if cover_path and Path(cover_path).exists():
            ext = Path(cover_path).suffix.lower() or '.jpg'
            zf.write(cover_path, f'cover{ext}')
    return buf.getvalue()
