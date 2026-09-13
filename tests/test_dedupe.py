"""Tests for feedpakr_dedupe.py (issue #49 — duplicate .feedpak detection
and safe cleanup). Pure filesystem/zip logic — no GP sample fixtures
needed, so these run everywhere the repo itself is importable."""

import zipfile
from pathlib import Path

import feedpakr_dedupe as dedupe


def _write_pack(path: Path, members: dict[str, bytes], date_time=(2024, 1, 1, 0, 0, 0)) -> None:
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=date_time)
            zf.writestr(info, data)


def test_compute_pack_content_hash_ignores_zip_metadata(tmp_path):
    """Two archives with identical member content but different zip
    timestamps (exactly what re-zipping the same content at a different
    time produces) must hash identically — that's the whole point of a
    content hash over member bytes rather than comparing files directly."""
    a = tmp_path / 'a.feedpak'
    b = tmp_path / 'b.feedpak'
    members = {'manifest.yaml': b'title: Song\n', 'stems/full.ogg': b'OggS-audio'}
    _write_pack(a, members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(b, members, date_time=(2025, 6, 15, 12, 30, 0))

    hash_a = dedupe.compute_pack_content_hash(a)
    hash_b = dedupe.compute_pack_content_hash(b)
    assert hash_a is not None
    assert hash_a == hash_b


def test_compute_pack_content_hash_differs_for_different_content(tmp_path):
    a = tmp_path / 'a.feedpak'
    b = tmp_path / 'b.feedpak'
    _write_pack(a, {'manifest.yaml': b'title: Song A\n'})
    _write_pack(b, {'manifest.yaml': b'title: Song B\n'})

    assert dedupe.compute_pack_content_hash(a) != dedupe.compute_pack_content_hash(b)


def test_compute_pack_content_hash_none_for_unreadable_archive(tmp_path):
    bogus = tmp_path / 'not_a_zip.feedpak'
    bogus.write_bytes(b'not a zip file at all')
    assert dedupe.compute_pack_content_hash(bogus) is None


def test_find_duplicate_feedpaks_groups_identical_packs(tmp_path):
    members = {'manifest.yaml': b'title: Song\n', 'stems/full.ogg': b'OggS'}
    _write_pack(tmp_path / 'Song.feedpak', members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(tmp_path / 'Song_2.feedpak', members, date_time=(2024, 6, 1, 0, 0, 0))
    _write_pack(tmp_path / 'Other.feedpak', {'manifest.yaml': b'title: Other\n'})

    groups = dedupe.find_duplicate_feedpaks(tmp_path)

    assert len(groups) == 1
    paths = {f['path'] for f in groups[0]['files']}
    assert paths == {'Song.feedpak', 'Song_2.feedpak'}
    # oldest-first ordering
    assert groups[0]['files'][0]['path'] == 'Song.feedpak'
    assert groups[0]['files'][1]['path'] == 'Song_2.feedpak'


def test_find_duplicate_feedpaks_no_duplicates_returns_empty(tmp_path):
    _write_pack(tmp_path / 'A.feedpak', {'manifest.yaml': b'title: A\n'})
    _write_pack(tmp_path / 'B.feedpak', {'manifest.yaml': b'title: B\n'})

    assert dedupe.find_duplicate_feedpaks(tmp_path) == []


def test_find_duplicate_feedpaks_ignores_trash_dir(tmp_path):
    """A file already moved to the trash folder by a previous cleanup must
    never be re-offered as a live duplicate candidate."""
    members = {'manifest.yaml': b'title: Song\n'}
    _write_pack(tmp_path / 'Song.feedpak', members)
    trash = tmp_path / dedupe.TRASH_DIRNAME
    trash.mkdir()
    _write_pack(trash / '20240101_Song_2.feedpak', members)

    assert dedupe.find_duplicate_feedpaks(tmp_path) == []


def test_find_duplicate_feedpaks_different_sizes_never_grouped(tmp_path):
    """Different member sizes fail the cheap fingerprint pre-check, so the
    (expensive) content hash is never even computed for them."""
    _write_pack(tmp_path / 'A.feedpak', {'manifest.yaml': b'short'})
    _write_pack(tmp_path / 'B.feedpak', {'manifest.yaml': b'a much longer manifest body here'})

    assert dedupe.find_duplicate_feedpaks(tmp_path) == []


# ── delete_duplicate_feedpaks ───────────────────────────────────────────────

def _make_duplicate_trio(tmp_path):
    members = {'manifest.yaml': b'title: Song\n'}
    a = tmp_path / 'Song.feedpak'
    b = tmp_path / 'Song_2.feedpak'
    c = tmp_path / 'Song_3.feedpak'
    _write_pack(a, members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(b, members, date_time=(2024, 2, 1, 0, 0, 0))
    _write_pack(c, members, date_time=(2024, 3, 1, 0, 0, 0))
    return a, b, c


def test_delete_moves_to_trash_not_unlink(tmp_path):
    a, b, c = _make_duplicate_trio(tmp_path)

    result = dedupe.delete_duplicate_feedpaks(tmp_path, ['Song_2.feedpak'])

    assert not b.exists()  # gone from its original location
    trashed = list((tmp_path / dedupe.TRASH_DIRNAME).iterdir())
    assert len(trashed) == 1
    assert result['results'] == [{
        'path': 'Song_2.feedpak',
        'trashed_to': f'{dedupe.TRASH_DIRNAME}/{trashed[0].name}',
    }]
    # original content preserved, just relocated
    with zipfile.ZipFile(trashed[0]) as zf:
        assert zf.read('manifest.yaml') == b'title: Song\n'


def test_delete_refuses_non_feedpak_extension(tmp_path):
    a, b, c = _make_duplicate_trio(tmp_path)
    sloppak = tmp_path / 'Song.sloppak'
    sloppak.write_bytes(b'must never be touched')

    result = dedupe.delete_duplicate_feedpaks(tmp_path, ['Song.sloppak'])

    assert sloppak.exists()
    assert sloppak.read_bytes() == b'must never be touched'
    assert result['results'][0]['error'] == 'Refusing to touch a non-.feedpak path.'


def test_delete_refuses_path_escaping_dlc_root(tmp_path):
    _make_duplicate_trio(tmp_path)
    outside = tmp_path.parent / 'outside.feedpak'
    outside.write_bytes(b'must never be touched')

    result = dedupe.delete_duplicate_feedpaks(tmp_path, ['../outside.feedpak'])

    assert outside.exists()
    assert 'escapes' in result['results'][0]['error']


def test_delete_refuses_last_remaining_copy_in_group(tmp_path):
    """A duplicate pair (not trio) must keep at least one member — deleting
    the second copy of a two-file group is refused."""
    members = {'manifest.yaml': b'title: Song\n'}
    a = tmp_path / 'Song.feedpak'
    b = tmp_path / 'Song_2.feedpak'
    _write_pack(a, members, date_time=(2024, 1, 1, 0, 0, 0))
    _write_pack(b, members, date_time=(2024, 2, 1, 0, 0, 0))

    # Delete one copy — fine, one survives.
    r1 = dedupe.delete_duplicate_feedpaks(tmp_path, ['Song_2.feedpak'])
    assert 'trashed_to' in r1['results'][0]
    assert a.exists()

    # Now try to delete the last surviving copy of that same (now-solo) file —
    # it's no longer part of any duplicate GROUP at all (only one file left),
    # so this hits the "no longer a detected duplicate" path, not the
    # last-copy guard specifically — either way, it must be refused.
    r2 = dedupe.delete_duplicate_feedpaks(tmp_path, ['Song.feedpak'])
    assert 'error' in r2['results'][0]
    assert a.exists()


def test_delete_refuses_emptying_a_group_across_multiple_paths_in_one_request(tmp_path):
    """The last-copy guard must hold even when a single request tries to
    remove every member of a group at once, not just one at a time."""
    a, b, c = _make_duplicate_trio(tmp_path)

    result = dedupe.delete_duplicate_feedpaks(
        tmp_path, ['Song.feedpak', 'Song_2.feedpak', 'Song_3.feedpak'],
    )

    outcomes = {r['path']: r for r in result['results']}
    succeeded = [p for p, r in outcomes.items() if 'trashed_to' in r]
    failed = [p for p, r in outcomes.items() if 'error' in r]
    assert len(succeeded) == 2
    assert len(failed) == 1
    # Exactly one of the three original files must still be on disk.
    survivors = [p for p in (a, b, c) if p.exists()]
    assert len(survivors) == 1
    assert failed[0] == survivors[0].name


def test_delete_refuses_stale_path_no_longer_a_real_duplicate(tmp_path):
    """Re-verifies against a fresh scan rather than trusting the caller —
    a lone .feedpak (never a duplicate of anything) must be refused even
    if the caller claims it came from a duplicate listing."""
    lone = tmp_path / 'Solo.feedpak'
    _write_pack(lone, {'manifest.yaml': b'title: Solo\n'})

    result = dedupe.delete_duplicate_feedpaks(tmp_path, ['Solo.feedpak'])

    assert lone.exists()
    assert 'No longer a detected duplicate' in result['results'][0]['error']


def test_delete_reports_missing_file_as_no_longer_a_duplicate(tmp_path):
    """A file removed before the delete call runs (e.g. a race with
    another process) is naturally absent from the fresh scan
    delete_duplicate_feedpaks takes internally — it never reaches the
    is_file() check, and gets the same 'no longer a duplicate' message a
    caller passing a stale/wrong path would (both are really the same
    situation: the path isn't a live duplicate right now)."""
    a, b, c = _make_duplicate_trio(tmp_path)
    b.unlink()

    result = dedupe.delete_duplicate_feedpaks(tmp_path, ['Song_2.feedpak'])

    assert 'No longer a detected duplicate' in result['results'][0]['error']
