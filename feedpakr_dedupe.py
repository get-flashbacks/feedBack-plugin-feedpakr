"""Duplicate .feedpak detection and safe cleanup (issue #49).

Re-upgrading a .sloppak before conflict_policy existed (or any other path
that leaves more than one .feedpak for the same source around) can leave
byte-different-but-content-identical .feedpak files behind — e.g.
`Song.feedpak` and `Song_2.feedpak` built from the same .sloppak at
different times. This module finds those groups by actual archive
*content* (not file size/mtime/name), and removes caller-selected members
of a group — never the group's oldest ("canonical") member, never
anything but a .feedpak, and never a hard delete.

Two-phase detection keeps this cheap for a real DLC library:
  1. `_cheap_fingerprint` reads only each zip's central directory (member
     names + uncompressed sizes) — no decompression, no full read.
  2. Only candidates that share a fingerprint (i.e. could plausibly be
     identical) get the expensive step: a real content hash that reads
     every member's bytes via feedpakr_upgrade's safe reader.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import zipfile
import zlib
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
    Returns None if the archive can't be read cleanly, contains an unsafe
    (path-escaping) member, or any single member fails to decompress
    (corrupt deflate stream raises zlib.error, not BadZipFile/OSError) —
    such a pack is never treated as a clean duplicate of anything, and a
    single corrupt file must never abort a whole-library scan, matching
    every other best-effort function in this plugin."""
    try:
        members, unsafe = upgrade.list_archive_members(path)
    except (zipfile.BadZipFile, OSError):
        return None
    if unsafe:
        return None
    digest = hashlib.sha256()
    for rel in sorted(members):
        try:
            raw = upgrade.read_archive_member(path, rel)
        except (zipfile.BadZipFile, OSError, zlib.error):
            return None
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

    `files` within a group is sorted oldest-first (mtime) — `files[0]` is
    the group's protected "canonical" member (see delete_duplicate_feedpaks).
    The trash folder is excluded so a previous cleanup's soft-deleted files
    are never re-offered.

    A `.feedpak` that is (or resolves through) a symlink pointing outside
    dlc_root is skipped rather than scanned — `rglob` doesn't distinguish
    real files from symlinks, and a naive scan would happily hash and
    report content that lives entirely outside the DLC folder.
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
        try:
            p.resolve().relative_to(dlc_root)
        except (OSError, ValueError):
            continue  # a symlink escaping dlc_root (or unresolvable) — never a candidate
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
    """A destination inside dlc_root/.feedpakr_trash/ that's unique without
    a check-then-act exists() loop: two concurrent deletes of files that
    share a basename (e.g. from different groups/subdirectories) landing
    in the same second used to be able to race a plain increment-and-check
    loop and silently overwrite one trashed copy with another — the one
    path where "never a hard delete" could actually lose data. A random
    token makes any collision astronomically unlikely regardless of
    timing, so there's nothing left to race. Doesn't create the trash
    directory itself — callers processing a batch should do that once
    up front (see delete_duplicate_feedpaks) rather than every call
    redundantly re-checking it."""
    trash_dir = dlc_root / TRASH_DIRNAME
    stem = Path(rel).name
    ts = time.strftime('%Y%m%dT%H%M%S')
    token = secrets.token_hex(4)
    return trash_dir / f'{ts}_{token}_{stem}'


def delete_duplicate_feedpaks(dlc_root: str | Path, rel_paths: list[str]) -> dict:
    """Soft-delete (move into `dlc_root/.feedpakr_trash/`, never unlink) a
    caller-selected set of .feedpak duplicates.

    Safety, enforced per-path and independent of whatever the caller
    claims:
      - never anything but a .feedpak path (a .sloppak, or any other
        extension, is rejected outright — not silently skipped, since a
        caller passing one is a bug worth surfacing)
      - never a path that escapes dlc_root (checked against the *resolved*
        path, so a symlink can't be used to reach outside it either)
      - re-verifies against a FRESH `find_duplicate_feedpaks` scan (not
        whatever the caller/UI last saw) that the path is still part of a
        real duplicate group — closes the race where the file changed
        between listing and deleting
      - never removes a group's oldest ("canonical") member — the one a
        clean re-upgrade would key off (list_sloppaks' already_upgraded
        check is `sloppak.with_suffix('.feedpak')`, which is exactly the
        clean, undecorated name a from-scratch conversion produces).
        Determining "oldest" this way, from a scan each call takes fresh,
        rather than tracking a shared "how many are left" counter across
        the request, also removes the concurrency hazard a counter has:
        two overlapping delete requests each recompute the same protected
        path independently, so no interleaving of concurrent requests can
        ever result in a group losing its canonical member, and a
        select-everything request can never delete more than
        (group size - 1) regardless of what order the paths arrive in.

    Recoverable, not permanent: moved into a DLC-local trash folder rather
    than deleted, since this plugin has no dependency on an OS trash/
    recycle-bin integration (send2trash et al.) — "recoverable deletion
    where supported" (issue #49) is satisfied by never calling unlink()
    for this operation at all.
    """
    dlc_root = Path(dlc_root).resolve()
    (dlc_root / TRASH_DIRNAME).mkdir(exist_ok=True)
    current_groups = find_duplicate_feedpaks(dlc_root)
    group_by_path: dict[str, dict] = {}
    protected_path_by_hash: dict[str, str] = {}
    for g in current_groups:
        protected_path_by_hash[g['hash']] = g['files'][0]['path']  # oldest-first
        for f in g['files']:
            group_by_path[f['path']] = g

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
        if rel == protected_path_by_hash[group['hash']]:
            results.append({
                'path': rel,
                'error': 'Refusing to remove the oldest copy in this group — '
                         'it is kept as the canonical version.',
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

        results.append({'path': rel, 'trashed_to': dest.relative_to(dlc_root).as_posix()})

    return {'results': results, 'trash_dir': TRASH_DIRNAME}
