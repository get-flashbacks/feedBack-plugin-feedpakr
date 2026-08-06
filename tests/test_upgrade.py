"""Tests for feedpakr_upgrade.py.

Synthetic dir-form sloppaks (built with tmp_path) cover the logic in
isolation; a handful of real-fixture regression tests at the bottom lock
in what was verified manually against every sample sloppak this project
has (c:\\Users\\PC\\Downloads\\Alll\\export) — 21/21 upgraded fully spec-valid.
"""

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

import feedpakr_upgrade as upgrade
import feedpakr_validate as validate

ffmpeg_available = pytest.mark.skipif(
    shutil.which('ffmpeg') is None, reason='ffmpeg not installed on this host'
)


def _write_sloppak(tmp_path: Path, manifest: dict, arrangements: dict[str, dict] | None = None) -> Path:
    """Build a minimal dir-form sloppak under tmp_path/'song.sloppak'."""
    src = tmp_path / 'song.sloppak'
    src.mkdir()
    (src / 'manifest.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')
    if arrangements:
        (src / 'arrangements').mkdir()
        for name, payload in arrangements.items():
            (src / 'arrangements' / name).write_text(json.dumps(payload), encoding='utf-8')
    return src


def _unzip(pak_bytes: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(pak_bytes))


def test_stamps_feedpak_version(tmp_path):
    src = _write_sloppak(tmp_path, {'title': 'T', 'artist': 'A', 'duration': 10.0,
                                     'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
                                     'arrangements': []})
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['feedpak_version'] == upgrade.FEEDPAK_VERSION


def test_original_file_never_modified(tmp_path):
    src = _write_sloppak(tmp_path, {'title': 'T', 'artist': 'A', 'duration': 10.0,
                                     'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': []})
    original_manifest_text = (src / 'manifest.yaml').read_text(encoding='utf-8')
    upgrade.upgrade_sloppak(str(src))
    assert (src / 'manifest.yaml').read_text(encoding='utf-8') == original_manifest_text


def test_preserves_unknown_top_level_keys(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': [],
        'some_future_key': {'nested': 'value'},
    })
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['some_future_key'] == {'nested': 'value'}


def test_copies_all_files_verbatim(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': [],
        'stem_separation': {'engine': 'demucs', 'model': 'htdemucs', 'version': '1.0.0'},
    })
    (src / 'stems').mkdir()
    (src / 'stems' / 'full.ogg').write_bytes(b'\x00\x01\x02fake-audio')
    (src / 'cover.png').write_bytes(b'\x89PNGfake')

    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        assert zf.read('stems/full.ogg') == b'\x00\x01\x02fake-audio'
        assert zf.read('cover.png') == b'\x89PNGfake'
    assert result['features']['real_audio'] is True


def test_upgrade_sloppak_rejects_path_traversal_members_in_zip_source(tmp_path):
    """A zip-form .sloppak with a member named to escape the archive
    (../.. or an absolute path) must not have that name copied verbatim
    into the newly-built .feedpak — that would forward a zip-slip payload
    to whatever later extracts the output file."""
    zip_path = tmp_path / 'song.sloppak'
    manifest = {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': [],
    }
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(manifest, sort_keys=False))
        zf.writestr('stems/full.ogg', b'OggS-full')
        zf.writestr('../../../../tmp/evil.txt', b'pwned')
        zf.writestr('/etc/evil.txt', b'pwned')

    result = upgrade.upgrade_sloppak(str(zip_path))
    with _unzip(result['bytes']) as zf:
        names = zf.namelist()
    assert not any(n.startswith('/') for n in names)
    assert not any('..' in Path(n).parts for n in names)
    assert 'stems/full.ogg' in names
    assert any('unsafe' in w.lower() for w in result['warnings'])


def test_midi_rendered_single_stem_disables_real_audio_feature(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
        'arrangements': [],
    })
    (src / 'stems').mkdir()
    (src / 'stems' / 'full.ogg').write_bytes(b'midi-rendered-audio')

    result = upgrade.upgrade_sloppak(str(src))

    assert result['features']['real_audio'] is False


def test_upgrade_without_complete_mix_disables_real_audio_feature(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [
            {'id': 'guitar', 'file': 'stems/guitar.ogg'},
            {'id': 'bass', 'file': 'stems/bass.ogg'},
        ],
        'arrangements': [],
    })
    (src / 'stems').mkdir()
    (src / 'stems' / 'guitar.ogg').write_bytes(b'guitar')
    (src / 'stems' / 'bass.ogg').write_bytes(b'bass')
    result = upgrade.upgrade_sloppak(str(src))
    assert result['features']['real_audio'] is False


# ── song_timeline promotion ─────────────────────────────────────────────────

def test_promotes_embedded_beats_and_sections_time_key(tmp_path):
    src = _write_sloppak(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'x.ogg'}],
         'arrangements': [{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}]},
        arrangements={'lead.json': {
            'name': 'Lead', 'notes': [],
            'beats': [{'time': 0.0, 'measure': 1}, {'time': 0.5, 'measure': -1}],
            'sections': [{'name': 'Verse', 'number': 1, 'time': 0.0}],
        }},
    )
    result = upgrade.upgrade_sloppak(str(src))
    assert any('song_timeline' in w for w in result['warnings'])
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        assert manifest['song_timeline'] == 'song_timeline.json'
        timeline = json.loads(zf.read('song_timeline.json'))
    assert timeline['beats'] == [{'time': 0.0, 'measure': 1}, {'time': 0.5, 'measure': -1}]
    assert timeline['sections'] == [{'name': 'Verse', 'number': 1, 'time': 0.0}]


def test_promotes_sections_with_start_time_key():
    """The editor plugin's save format uses `start_time` instead of `time`
    for sections — both variants must be accepted."""
    payload = {'name': 'Lead', 'notes': [], 'beats': [],
               'sections': [{'name': 'Chorus', 'number': 1, 'start_time': 12.5}]}
    manifest = {'arrangements': [{'id': 'lead', 'file': 'arrangements/lead.json'}]}

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / 'arrangements').mkdir()
        (d / 'arrangements' / 'lead.json').write_text(json.dumps(payload), encoding='utf-8')
        timeline = upgrade._build_song_timeline(manifest, d)
    assert timeline['sections'] == [{'name': 'Chorus', 'number': 1, 'time': 12.5}]


def test_no_embedded_timeline_no_song_timeline_key(tmp_path):
    src = _write_sloppak(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'x.ogg'}],
         'arrangements': [{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}]},
        arrangements={'lead.json': {'name': 'Lead', 'notes': []}},
    )
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        assert 'song_timeline.json' not in zf.namelist()
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert 'song_timeline' not in manifest


def test_existing_song_timeline_key_not_overwritten(tmp_path):
    src = _write_sloppak(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'x.ogg'}],
         'song_timeline': 'song_timeline.json',
         'arrangements': [{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}]},
        arrangements={'lead.json': {'name': 'Lead', 'notes': [],
                                     'beats': [{'time': 0.0, 'measure': 1}]}},
    )
    (src / 'song_timeline.json').write_text(json.dumps({'version': 1, 'beats': [{'time': 99.0, 'measure': 1}]}), encoding='utf-8')

    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        timeline = json.loads(zf.read('song_timeline.json'))
    # The real, pre-existing file must survive untouched — not be
    # overwritten by a re-derived summary from the arrangement JSON.
    assert timeline['beats'][0]['time'] == 99.0


# ── full-stem promotion ──────────────────────────────────────────────────

def test_promotes_original_audio_to_full_stem(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'vocals', 'file': 'stems/vocals.ogg'}, {'id': 'guitar', 'file': 'stems/guitar.ogg'}],
        'original_audio': 'original/full.ogg',
        'arrangements': [],
    })
    (src / 'original').mkdir()
    (src / 'original' / 'full.ogg').write_bytes(b'fake')

    result = upgrade.upgrade_sloppak(str(src))
    assert any('original_audio' in w for w in result['warnings'])
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert 'original_audio' not in manifest
    full = next(s for s in manifest['stems'] if s['id'] == 'full')
    assert full['file'] == 'original/full.ogg'


def test_single_non_full_stem_renamed_to_full(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'audio', 'file': 'stems/audio.mp3', 'default': 'on'}],
        'arrangements': [],
    })
    result = upgrade.upgrade_sloppak(str(src))
    assert any('Renamed' in w for w in result['warnings'])
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['stems'] == [{'id': 'full', 'file': 'stems/audio.mp3', 'default': 'on'}]


def test_multi_stem_no_full_warns_without_fabricating(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'vocals', 'file': 'v.ogg'}, {'id': 'guitar', 'file': 'g.ogg'}],
        'arrangements': [],
    })
    result = upgrade.upgrade_sloppak(str(src))
    assert any('no complete mix' in w for w in result['warnings'])
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert not any(s.get('id') == 'full' for s in manifest['stems'])


def test_already_has_full_stem_untouched(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
        'arrangements': [],
    })
    result = upgrade.upgrade_sloppak(str(src))
    assert not any('Renamed' in w or 'original_audio' in w or 'no complete mix' in w
                   for w in result['warnings'])


# ── lyrics_source normalization ─────────────────────────────────────────────

def test_lyrics_source_xml_corrected_to_authored(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': [],
        'lyrics': 'lyrics.json', 'lyrics_source': 'xml',
    })
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['lyrics_source'] == 'authored'


def test_lyrics_source_sng_corrected_to_authored(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': [],
        'lyrics': 'lyrics.json', 'lyrics_source': 'sng',
    })
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['lyrics_source'] == 'authored'


def test_lyrics_source_engine_name_corrected_via_transcription_block(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': [],
        'lyrics': 'lyrics.json', 'lyrics_source': 'whisperx',
        'lyric_transcription': {'engine': 'whisperx', 'model': 'medium', 'version': '1.0.0'},
    })
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['lyrics_source'] == 'transcribed'


def test_lyrics_source_unknown_without_transcription_left_alone(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': [],
        'lyrics': 'lyrics.json', 'lyrics_source': 'some_mystery_tool',
    })
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['lyrics_source'] == 'some_mystery_tool'
    assert any('some_mystery_tool' in w for w in result['warnings'])


def test_valid_lyrics_source_untouched(tmp_path):
    src = _write_sloppak(tmp_path, {
        'title': 'T', 'artist': 'A', 'duration': 10.0,
        'stems': [{'id': 'full', 'file': 'x.ogg'}], 'arrangements': [],
        'lyrics': 'lyrics.json', 'lyrics_source': 'user',
    })
    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['lyrics_source'] == 'user'


# ── full pipeline: fully-fixed pack validates clean ─────────────────────────

def test_fully_fixable_pack_ends_up_schema_valid(tmp_path):
    src = _write_sloppak(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'audio', 'file': 'stems/audio.ogg'}],
         'lyrics': 'lyrics.json', 'lyrics_source': 'xml',
         'arrangements': [{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json',
                            'tuning': [0] * 6, 'capo': 0}]},
        arrangements={'lead.json': {
            'name': 'Lead', 'tuning': [0] * 6, 'capo': 0,
            'notes': [{'t': 0.0, 's': 0, 'f': 3}], 'chords': [], 'anchors': [],
            'handshapes': [], 'templates': [],
            'beats': [{'time': 0.0, 'measure': 1}],
            'sections': [{'name': 'Verse', 'number': 1, 'time': 0.0}],
        }},
    )
    result = upgrade.upgrade_sloppak(str(src))
    assert result['validation'] == {}


# ── extract_pack_assets (feedpakr's 'existing_pack' audio mode) ────────────

def _write_sloppak_with_files(tmp_path: Path, manifest: dict, files: dict[str, bytes]) -> Path:
    """Like _write_sloppak, but also writes real byte content for every
    manifest-referenced file (stems, cover) so extract_pack_assets has
    something to actually read."""
    src = tmp_path / 'song.sloppak'
    src.mkdir()
    (src / 'manifest.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')
    for rel, data in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return src


def test_extract_pack_assets_full_mix_only(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': []},
        {'stems/full.ogg': b'OggS-full-mix'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is None
    assert result['stems'] == []
    assert Path(result['full_mix_path']).read_bytes() == b'OggS-full-mix'
    # A full mix is used directly as the autosync reference — no mixdown needed.
    assert result['sync_reference_path'] == result['full_mix_path']


@ffmpeg_available
def test_extract_pack_assets_separated_stems_no_full_mix(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [
             {'id': 'guitar', 'file': 'stems/guitar.ogg', 'name': 'Guitar'},
             {'id': 'vocals', 'file': 'stems/vocals.ogg'},
         ], 'arrangements': []},
        {'stems/guitar.ogg': b'OggS-guitar', 'stems/vocals.ogg': b'OggS-vocals'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is None
    assert result['full_mix_path'] is None
    ids = {s['id'] for s in result['stems']}
    assert ids == {'guitar', 'vocals'}
    guitar_entry = next(s for s in result['stems'] if s['id'] == 'guitar')
    assert guitar_entry['name'] == 'Guitar'
    assert Path(guitar_entry['file']).read_bytes() == b'OggS-guitar'
    # No 'full' mixdown to reuse as the sync reference — a mixdown gets
    # built instead (single-stem shortcut here would just copy, but with
    # two stems this exercises the ffmpeg amix path or its failure).
    assert result['sync_reference_path'] is not None


def test_extract_pack_assets_single_separated_stem_copies_for_sync(tmp_path):
    """With exactly one non-'full' stem, the sync reference is a plain
    copy — no ffmpeg mixdown needed."""
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'guitar', 'file': 'stems/guitar.ogg'}], 'arrangements': []},
        {'stems/guitar.ogg': b'OggS-solo-guitar'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is None
    assert Path(result['sync_reference_path']).read_bytes() == b'OggS-solo-guitar'


def test_extract_pack_assets_extracts_cover(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'stems/full.ogg'}],
         'cover': 'cover.jpg', 'arrangements': []},
        {'stems/full.ogg': b'OggS-full', 'cover.jpg': b'\xff\xd8\xff-jpeg-bytes'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is None
    assert Path(result['cover_path']).read_bytes() == b'\xff\xd8\xff-jpeg-bytes'


def test_extract_pack_assets_cover_does_not_overwrite_stem_basename(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'stems/cover.jpg'}],
         'cover': 'art/cover.jpg', 'arrangements': []},
        {'stems/cover.jpg': b'OggS-full', 'art/cover.jpg': b'\xff\xd8\xff-cover'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is None
    assert Path(result['full_mix_path']).read_bytes() == b'OggS-full'
    assert Path(result['cover_path']).read_bytes() == b'\xff\xd8\xff-cover'
    assert Path(result['cover_path']).name != Path(result['full_mix_path']).name


def test_extract_pack_assets_no_cover_key_yields_none(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': []},
        {'stems/full.ogg': b'OggS-full'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['cover_path'] is None


def test_extract_pack_assets_no_stems_errors(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0, 'stems': [], 'arrangements': []},
        {},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is not None
    assert 'no stems' in result['error'].lower()


def test_extract_pack_assets_missing_manifest_errors(tmp_path):
    missing = tmp_path / 'does-not-exist.feedpak'
    result = upgrade.extract_pack_assets(missing, tmp_path / 'out')
    assert result['error'] is not None


def test_extract_pack_assets_caps_decompressed_member_size(tmp_path, monkeypatch):
    """A declared stem whose decompressed content exceeds the per-member
    cap must be skipped (as if unreadable), not fully buffered in memory —
    guards against a zip-bomb entry disguised as a small upload.

    Forces the module's own zip-fallback code path (sloppak_mod=None) —
    when the host's real sloppak module is importable (e.g. a full host
    checkout sits on sys.path, as some dev/test environments have), it
    would otherwise service the read itself via read_member_bytes(),
    which isn't code this test is exercising."""
    monkeypatch.setattr(upgrade, 'sloppak_mod', None)
    monkeypatch.setattr(upgrade, '_MAX_MEMBER_BYTES', 1024)
    zip_path = tmp_path / 'song.feedpak'
    manifest = {'title': 'T', 'artist': 'A', 'duration': 10.0,
                'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': []}
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(manifest, sort_keys=False))
        # Highly compressible payload: tiny on disk, well over the cap once
        # decompressed — stands in for a real zip-bomb entry.
        zf.writestr('stems/full.ogg', b'\x00' * 1024 * 1024)

    result = upgrade.extract_pack_assets(zip_path, tmp_path / 'out')
    assert result['error'] is not None or result.get('warnings')
    if result['error'] is None:
        assert result['full_mix_path'] is None
        assert any('full' in w.lower() for w in result['warnings'])


def test_load_manifest_fallback_caps_decompressed_zip_manifest(tmp_path, monkeypatch):
    """manifest.yaml itself is the first thing read off an untrusted upload,
    before any other size check runs — a small, highly-compressible
    manifest.yaml entry must not be fully buffered in memory past the
    per-member cap (a zip-bomb manifest, not just a zip-bomb stem)."""
    monkeypatch.setattr(upgrade, '_MAX_MEMBER_BYTES', 1024)
    zip_path = tmp_path / 'song.sloppak'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Highly compressible payload: tiny on disk, well over the cap once
        # decompressed — stands in for a real zip-bomb manifest entry.
        zf.writestr('manifest.yaml', b'a: ' + b'x' * (1024 * 1024))

    with pytest.raises(FileNotFoundError):
        upgrade._load_manifest_fallback(zip_path)


def test_load_manifest_fallback_caps_oversized_dir_manifest(tmp_path, monkeypatch):
    """Directory-form counterpart of the above: an oversized manifest.yaml
    file on disk must also be declined rather than read in full."""
    monkeypatch.setattr(upgrade, '_MAX_MEMBER_BYTES', 1024)
    src = tmp_path / 'song.sloppak'
    src.mkdir()
    (src / 'manifest.yaml').write_bytes(b'a: ' + b'x' * (2 * 1024))

    with pytest.raises(FileNotFoundError):
        upgrade._load_manifest_fallback(src)


def test_load_manifest_fallback_zip_form_still_works_under_cap(tmp_path):
    """Sanity check: a normal small manifest.yaml is unaffected by the cap."""
    zip_path = tmp_path / 'song.sloppak'
    manifest = {'title': 'T', 'artist': 'A', 'duration': 10.0,
                'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': []}
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(manifest, sort_keys=False))

    assert upgrade._load_manifest_fallback(zip_path) == manifest


def test_extract_pack_assets_zip_form(tmp_path):
    """Works against a zip-form .feedpak, not just a dir-form .sloppak."""
    zip_path = tmp_path / 'song.feedpak'
    manifest = {'title': 'T', 'artist': 'A', 'duration': 10.0,
                'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': []}
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('manifest.yaml', yaml.safe_dump(manifest, sort_keys=False))
        zf.writestr('stems/full.ogg', b'OggS-zipped-full')

    result = upgrade.extract_pack_assets(zip_path, tmp_path / 'out')
    assert result['error'] is None
    assert Path(result['full_mix_path']).read_bytes() == b'OggS-zipped-full'


def test_extract_pack_assets_sanitizes_stem_ids_before_collision_names(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [
             {'id': 'full', 'file': 'mix/full.ogg'},
             {'id': 'guitar', 'file': 'a/audio.ogg'},
             {'id': '../lead', 'file': 'b/audio.ogg'},
         ], 'arrangements': []},
        {
            'mix/full.ogg': b'OggS-full',
            'a/audio.ogg': b'OggS-guitar',
            'b/audio.ogg': b'OggS-lead',
        },
    )
    out = tmp_path / 'out'
    result = upgrade.extract_pack_assets(src, out)
    assert result['error'] is None
    assert {s['id'] for s in result['stems']} == {'guitar', 'lead'}

    extracted = [Path(s['file']) for s in result['stems']]
    assert all(p.parent == out for p in extracted)
    assert {p.name for p in extracted} == {'audio.ogg', 'lead_audio.ogg'}
    assert all('..' not in p.parts for p in extracted)
    assert (out / 'lead_audio.ogg').read_bytes() == b'OggS-lead'


def test_extract_pack_assets_warns_when_declared_stem_cannot_be_read(tmp_path):
    src = _write_sloppak_with_files(
        tmp_path,
        {'title': 'T', 'artist': 'A', 'duration': 10.0,
         'stems': [
             {'id': 'full', 'file': 'stems/full.ogg'},
             {'id': 'missing', 'file': 'stems/missing.ogg'},
         ], 'arrangements': []},
        {'stems/full.ogg': b'OggS-full'},
    )
    result = upgrade.extract_pack_assets(src, tmp_path / 'out')
    assert result['error'] is None
    assert result['warnings']
    assert 'missing' in result['warnings'][0]


# ── Real-fixture regression (the actual sample library) ────────────────────

FIXTURE_DIR = Path(r'c:\Users\PC\Downloads\Alll\export')
fixture_available = pytest.mark.skipif(
    not FIXTURE_DIR.is_dir(), reason='sample sloppak library not present on this machine'
)


@fixture_available
def test_real_fixture_money_v2_ends_up_fully_valid():
    p = FIXTURE_DIR / 'Money_Pink Floyd_v2.sloppak'
    if not p.is_file():
        pytest.skip('fixture not present')
    result = upgrade.upgrade_sloppak(str(p))
    assert result['validation'] == {}
    assert any('song_timeline' in w for w in result['warnings'])
    assert any('whisperx' in w and 'transcribed' in w for w in result['warnings'])


@fixture_available
def test_real_fixture_amy_winehouse_v2_no_full_mix_honestly_reported():
    p = FIXTURE_DIR / 'sloppak' / 'Amy-Winehouse_Rehab_v2.sloppak'
    if not p.is_file():
        pytest.skip('fixture not present')
    result = upgrade.upgrade_sloppak(str(p))
    assert any('no complete mix' in w for w in result['warnings'])
    assert not result['validation']  # no full mix isn't a schema error — a permissible library shape


@fixture_available
def test_real_fixture_batch_all_fully_valid():
    """Regression-locks the manual batch check: every sample sloppak this
    project has upgrades to a fully spec-valid .feedpak."""
    paths = [p for p in FIXTURE_DIR.rglob('*.sloppak') if not p.name.endswith('.bak')]
    if not paths:
        pytest.skip('no sloppak fixtures found')
    failures = []
    for p in paths:
        result = upgrade.upgrade_sloppak(str(p))
        if result['validation']:
            failures.append((p.name, result['validation']))
    assert not failures, f'{len(failures)}/{len(paths)} fixtures did not validate: {failures}'
