"""Unit tests for feedpakr_pack.py — pure dict/zip logic, no pyguitarpro
or host core lib required."""

import io
import json
import zipfile

import yaml

import feedpakr_pack as pack


def test_sanitize_filename_component_strips_unsafe_chars():
    assert pack.sanitize_filename_component('My/Song: "Live"?') == 'My_Song_Live'


def test_sanitize_filename_component_empty_falls_back():
    assert pack.sanitize_filename_component('   ') == 'untitled'


def test_sanitize_filename_component_truncates():
    assert len(pack.sanitize_filename_component('x' * 200, max_len=10)) == 10


def test_unique_output_path_avoids_collision(tmp_path):
    p1 = pack.unique_output_path(tmp_path, 'Song')
    p1.write_bytes(b'x')
    p2 = pack.unique_output_path(tmp_path, 'Song')
    assert p1 != p2
    assert p2.name == 'Song_2.feedpak'


def test_arrangement_id_for_dedups():
    taken = set()
    assert pack.arrangement_id_for('Lead', taken) == 'lead'
    assert pack.arrangement_id_for('Lead', taken) == 'lead2'
    assert pack.arrangement_id_for('Lead!!', taken) == 'lead3'


def test_assemble_manifest_omits_stems_when_no_audio():
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0,
        arrangements=[{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}],
        stem_file=None,
    )
    assert manifest['stems'] == []
    assert manifest['feedpak_version'] == pack.FEEDPAK_VERSION


def test_assemble_manifest_includes_full_stem_when_audio_present():
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0,
        arrangements=[{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}],
        stem_file='stems/full.ogg',
    )
    assert manifest['stems'] == [{'id': 'full', 'file': 'stems/full.ogg', 'default': True}]


def test_assemble_manifest_omits_optional_keys_when_absent():
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0, arrangements=[], stem_file=None,
    )
    assert 'album' not in manifest
    assert 'year' not in manifest
    assert 'song_timeline' not in manifest
    assert 'lyrics' not in manifest
    assert 'cover' not in manifest


def test_assemble_manifest_normalizes_authors():
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0, arrangements=[], stem_file=None,
        authors=[' Alice ', '   ', '', 'Bob'],
    )
    assert manifest['authors'] == [{'name': 'Alice'}, {'name': 'Bob'}]

    blank = pack.assemble_manifest(
        title='T', artist='A', duration=10.0, arrangements=[], stem_file=None,
        authors=[' ', '\t'],
    )
    assert 'authors' not in blank


def test_assemble_manifest_includes_cover_when_present():
    """Regression test: write_feedpak_zip happily writes cover.jpg into the
    zip given a cover_path, but per spec §2.2 ("nothing is auto-discovered
    by scanning; a Reader MUST NOT rely on filename") that file is invisible
    to a compliant Reader unless the manifest's own `cover` key points at
    it. assemble_manifest was missing a cover_file parameter entirely, so
    every feedpakr-built pack with cover art shipped a cover.jpg no reader
    would ever show — caught via a real user-reported "no cover" symptom
    against packs that, on disk, did contain a cover image."""
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0,
        arrangements=[{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}],
        stem_file=None,
        cover_file='cover.jpg',
    )
    assert manifest['cover'] == 'cover.jpg'


def test_write_feedpak_zip_roundtrip(tmp_path):
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=5.0,
        arrangements=[{'id': 'lead', 'name': 'Lead', 'file': 'arrangements/lead.json'}],
        stem_file=None,
        song_timeline_present=True,
    )
    arrangement_files = {'lead.json': {'name': 'Lead', 'notes': []}}
    song_timeline = {'version': 1, 'sections': [{'name': 'Verse', 'number': 1, 'time': 0.0}]}

    pak_bytes = pack.write_feedpak_zip(
        manifest=manifest,
        arrangement_files=arrangement_files,
        song_timeline=song_timeline,
    )

    with zipfile.ZipFile(io.BytesIO(pak_bytes)) as zf:
        names = set(zf.namelist())
        assert names == {'manifest.yaml', 'arrangements/lead.json', 'song_timeline.json'}

        loaded_manifest = yaml.safe_load(zf.read('manifest.yaml'))
        assert loaded_manifest['title'] == 'T'

        loaded_arr = json.loads(zf.read('arrangements/lead.json'))
        assert loaded_arr['name'] == 'Lead'

        loaded_timeline = json.loads(zf.read('song_timeline.json'))
        assert loaded_timeline['sections'][0]['name'] == 'Verse'


def test_write_feedpak_zip_audio_extension_follows_source(tmp_path):
    audio_path = tmp_path / 'audio.wav'
    audio_path.write_bytes(b'RIFF....WAVEfmt ')

    pak_bytes = pack.write_feedpak_zip(
        manifest={'title': 'T'},
        arrangement_files={},
        audio_path=str(audio_path),
    )
    with zipfile.ZipFile(io.BytesIO(pak_bytes)) as zf:
        assert 'stems/full.wav' in zf.namelist()


def test_assemble_manifest_extra_stems_appended_after_full():
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0,
        arrangements=[],
        stem_file='stems/full.ogg',
        extra_stems=[
            {'id': 'guitar', 'file': 'stems/guitar.ogg', 'name': 'Guitar'},
            {'id': 'vocals', 'file': 'stems/vocals.ogg'},
        ],
    )
    assert manifest['stems'] == [
        {'id': 'full', 'file': 'stems/full.ogg', 'default': True},
        {'id': 'guitar', 'file': 'stems/guitar.ogg', 'name': 'Guitar'},
        {'id': 'vocals', 'file': 'stems/vocals.ogg'},
    ]


def test_assemble_manifest_extra_stems_without_full_mix():
    """'existing_pack' packs with only separated stems (no reserved 'full'
    mixdown) still get a valid, non-empty stems list."""
    manifest = pack.assemble_manifest(
        title='T', artist='A', duration=10.0,
        arrangements=[],
        stem_file=None,
        extra_stems=[{'id': 'guitar', 'file': 'stems/guitar.ogg'}],
    )
    assert manifest['stems'] == [{'id': 'guitar', 'file': 'stems/guitar.ogg'}]


def test_write_feedpak_zip_extra_stem_paths_copied_verbatim(tmp_path):
    guitar_path = tmp_path / 'guitar.ogg'
    guitar_path.write_bytes(b'OggS-guitar')
    vocals_path = tmp_path / 'vocals.ogg'
    vocals_path.write_bytes(b'OggS-vocals')

    pak_bytes = pack.write_feedpak_zip(
        manifest={'title': 'T'},
        arrangement_files={},
        extra_stem_paths=[
            (str(guitar_path), 'stems/guitar.ogg'),
            (str(vocals_path), 'stems/vocals.ogg'),
        ],
    )
    with zipfile.ZipFile(io.BytesIO(pak_bytes)) as zf:
        names = set(zf.namelist())
        assert {'stems/guitar.ogg', 'stems/vocals.ogg'} <= names
        assert zf.read('stems/guitar.ogg') == b'OggS-guitar'
        assert zf.read('stems/vocals.ogg') == b'OggS-vocals'


def test_write_feedpak_zip_full_mix_and_extra_stems_coexist(tmp_path):
    full_path = tmp_path / 'full.ogg'
    full_path.write_bytes(b'OggS-full')
    guitar_path = tmp_path / 'guitar.ogg'
    guitar_path.write_bytes(b'OggS-guitar')

    pak_bytes = pack.write_feedpak_zip(
        manifest={'title': 'T'},
        arrangement_files={},
        audio_path=str(full_path),
        extra_stem_paths=[(str(guitar_path), 'stems/guitar.ogg')],
    )
    with zipfile.ZipFile(io.BytesIO(pak_bytes)) as zf:
        names = set(zf.namelist())
        assert {'stems/full.ogg', 'stems/guitar.ogg'} <= names
