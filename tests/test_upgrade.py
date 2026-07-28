"""Tests for feedpakr_upgrade.py.

Synthetic dir-form sloppaks (built with tmp_path) cover the logic in
isolation; a handful of real-fixture regression tests at the bottom lock
in what was verified manually against every sample sloppak this project
has (c:\\Users\\PC\\Downloads\\Alll\\export) — 21/21 upgraded fully spec-valid.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

import feedpakr_upgrade as upgrade
import feedpakr_validate as validate


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
    src = _write_sloppak(tmp_path, {'title': 'T', 'artist': 'A', 'duration': 10.0,
                                     'stems': [{'id': 'full', 'file': 'stems/full.ogg'}], 'arrangements': []})
    (src / 'stems').mkdir()
    (src / 'stems' / 'full.ogg').write_bytes(b'\x00\x01\x02fake-audio')
    (src / 'cover.png').write_bytes(b'\x89PNGfake')

    result = upgrade.upgrade_sloppak(str(src))
    with _unzip(result['bytes']) as zf:
        assert zf.read('stems/full.ogg') == b'\x00\x01\x02fake-audio'
        assert zf.read('cover.png') == b'\x89PNGfake'


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
