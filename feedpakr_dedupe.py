"""Duplicate .feedpak detection and safe cleanup (issue #49).

Re-upgrading a .sloppak before conflict_policy existed (or any other path
that leaves more than one .feedpak for the same source around) can leave
byte-different-but-content-identical .feedpak files behind — e.g.
`Song.feedpak` and `Song_2.feedpak` built from the same .sloppak at
different times. This module finds those groups by actual archive
*content* (not file size/mtime/name), and removes caller-selected members
of a group — never a whole group, never anything but a .feedpak, and
never a hard delete.

Two-phase detection keeps this cheap for a real DLC library:
  1. `_cheap_fingerprint` reads only each zip's central directory (member
     names + uncompressed sizes) — no decompression, no full read.
  2. Only candidates that share a fingerprint (i.e. could plausibly be
     identical) get the expensive step: a real content hash that reads
     every member's bytes via feedpakr_upgrade's safe reader.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path

import feedpakr_upgrade as upgrade

TRASH_DIRNAME = '.feedpakr_trash'


def _cheap_fingerprint(path: Path) -> tuple | None:
    """(member name, uncompressed size) pairs, sorted. Reads only the zip's
    central directory — cheap. Two packs with different fingerprints can
    never have identical content, so this narrows the expensive full-hash
    comparison below to genuine candidates only. None on any read failure
    (corrupt archive, directory-form pack `zipfile` can't open, etc.) —
    such a file is never treated as a duplicate of anything."""
    try:
        with zipfile.ZipFile(path) as zf:
            return tuple(sorted(
                (zi.filename, zi.file_size) for zi in zf.infolist()
                if not zi.filename.endswith('/')
            ))
    except (zipfile.BadZipFile, OSError):
        return None


def compute_pack_content_hash(path: Path) -> str | None:
    """SHA-256 over the pack's actual member contents (name -> bytes),
    independent of zip metadata (timestamps, compression method/level,
    member order) — two packs built from the same source at different
    times hash identically iff their real content is byte-identical.
    Returns None if the archive can't be read cleanly, or contains an
    unsafe (path-escaping) member — such a pack is never treated as a
    clean duplicate of anything, matching every other best-effort function
    in this plugin."""
    try:
        members, unsafe = upgrade.list_archive_members(path)
    except (zipfile.BadZipFile, OSError):
        return None
    if unsafe:
        return None
    digest = hashlib.sha256()
    for rel in sorted(members):
        raw = upgrade.read_archive_member(path, rel)
        if raw is None:
            return None
        digest.update(rel.encode('utf-8'))
        digest.update(b'\0')
        digest.update(hashlib.sha256(raw).digest())
        digest.update(b'\0')
    return digest.hexdigest()


def find_duplicate_feedpaks(dlc_root: str | Path) -> list[dict]:
    """Scan every .feedpak under dlc_root and group ones with identical
    archive content. Returns only groups with 2+ members:

        [{'hash': str, 'files': [{'path', 'size', 'mtime'}, ...]}, ...]

    `files` within a group is sorted oldest-first (mtime) — a UI can use
    that ordering to suggest "keep the oldest" without this function
    imposing any deletion policy itself. The trash folder is excluded so a
    previous cleanup's soft-deleted files are never re-offered.
    """
    dlc_root = Path(dlc_root).resolve()
    trash_dir = dlc_root / TRASH_DIRNAME

    by_fingerprint: dict[tuple, list[Path]] = {}
    for p in dlc_root.rglob('*.feedpak'):
        if not p.is_file():
            continue
        try:
            p.relative_to(trash_dir)
            continue  # inside the trash folder — never a live duplicate candidate
        except ValueError:
            pass
        fp = _cheap_fingerprint(p)
        if fp is None:
            continue
        by_fingerprint.setdefault(fp, []).append(p)

    groups: list[dict] = []
    for candidates in by_fingerprint.values():
        if len(candidates) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for p in candidates:
            h = compute_pack_content_hash(p)
            if h is None:
                continue
            by_hash.setdefault(h, []).append(p)
        for h, dup_paths in by_hash.items():
            if len(dup_paths) < 2:
                continue
            files = []
            for p in sorted(dup_paths, key=lambda x: x.stat().st_mtime):
                st = p.stat()
                files.append({
                    'path': p.relative_to(dlc_root).as_posix(),
                    'size': st.st_size,
                    'mtime': st.st_mtime,
                })
            groups.append({'hash': h, 'files': files})

    groups.sort(key=lambda g: g['files'][0]['path'])
    return groups


def _trash_destination(dlc_root: Path, rel: str) -> Path:
    trash_dir = dlc_root / TRASH_DIRNAME
    trash_dir.mkdir(exist_ok=True)
    stem = Path(rel).name
    ts = time.strftime('%Y%m%dT%H%M%S')
    candidate = trash_dir / f'{ts}_{stem}'
    n = 2
    while candidate.exists():
        candidate = trash_dir / f'{ts}_{n}_{stem}'
        n += 1
    return candidate


def delete_duplicate_feedpaks(dlc_root: str | Path, rel_paths: list[str]) -> dict:
    """Soft-delete (move into `dlc_root/.feedpakr_trash/`, never unlink) a
    caller-selected set of .feedpak duplicates.

    Safety, enforced per-path and independent of whatever the caller
    claims:
      - never anything but a .feedpak path (a .sloppak, or any other
        extension, is rejected outright — not silently skipped, since a
        caller passing one is a bug worth surfacing)
      - never a path that escapes dlc_root
      - re-verifies against a FRESH `find_duplicate_feedpaks` scan (not
        whatever the caller/UI last saw) that the path is still part of a
        real duplicate group — closes the race where the file changed
        between listing and deleting
      - never empties a group entirely, even across multiple paths in one
        request — at least one member of every duplicate group always
        survives

    Recoverable, not permanent: moved into a DLC-local trash folder rather
    than deleted, since this plugin has no dependency on an OS trash/
    recycle-bin integration (send2trash et al.) — "recoverable deletion
    where supported" (issue #49) is satisfied by never calling unlink()
    for this operation at all.
    """
    dlc_root = Path(dlc_root).resolve()
    current_groups = find_duplicate_feedpaks(dlc_root)
    group_by_path: dict[str, dict] = {}
    for g in current_groups:
        for f in g['files']:
            group_by_path[f['path']] = g
    remaining_in_group = {g['hash']: len(g['files']) for g in current_groups}

    results = []
    for rel in rel_paths:
        if Path(rel).suffix.lower() != '.feedpak':
            results.append({'path': rel, 'error': 'Refusing to touch a non-.feedpak path.'})
            continue
        target = (dlc_root / rel).resolve()
        try:
            target.relative_to(dlc_root)
        except ValueError:
            results.append({'path': rel, 'error': 'Path escapes the DLC folder.'})
            continue

        group = group_by_path.get(rel)
        if group is None:
            results.append({
                'path': rel,
                'error': 'No longer a detected duplicate (rescan and try again).',
            })
            continue
        if remaining_in_group[group['hash']] <= 1:
            results.append({
                'path': rel,
                'error': 'Refusing to remove the last remaining copy in this group.',
            })
            continue
        if not target.is_file():
            results.append({'path': rel, 'error': 'File no longer exists.'})
            continue

        try:
            dest = _trash_destination(dlc_root, rel)
            target.rename(dest)
        except OSError as e:
            results.append({'path': rel, 'error': str(e)})
            continue

        remaining_in_group[group['hash']] -= 1
        results.append({'path': rel, 'trashed_to': dest.relative_to(dlc_root).as_posix()})

    return {'results': results, 'trash_dir': TRASH_DIRNAME}
