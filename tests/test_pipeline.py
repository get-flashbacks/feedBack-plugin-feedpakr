"""Tests for feedpakr_pipeline.py. Needs the feedBack host core lib
(guitarpro, gp2rs, song, gp2midi) on sys.path — see conftest.py — and,
for the fixture-backed tests, the c:\\Users\\PC\\Downloads\\Alll sample
folder. Both self-skip cleanly when unavailable rather than false-failing
in an environment that only has this one repo checked out.
"""

from pathlib import Path

import pytest
import yaml

import feedpakr_pipeline as pipeline

HOST_AVAILABLE = (
    pipeline.guitarpro is not None
    and pipeline.gp2rs is not None
    and pipeline.gp2rs_gpx is not None
)

pytestmark = pytest.mark.skipif(
    not HOST_AVAILABLE, reason='feedBack host core lib not on sys.path'
)

FIXTURE_DIR = Path(r'c:\Users\PC\Downloads\Alll')
MONEY_GP5 = FIXTURE_DIR / 'Money (J).gp5'
MONEY_GP8 = FIXTURE_DIR / 'Money (J).gp'
fixture_available = pytest.mark.skipif(
    not MONEY_GP5.is_file(), reason='sample fixture not present on this machine'
)
gp8_fixture_available = pytest.mark.skipif(
    not MONEY_GP8.is_file(), reason='GP8 sample fixture not present on this machine'
)


def test_check_extension_accepts_gpx_and_gp():
    pipeline._check_extension('song.gpx')  # must not raise
    pipeline._check_extension('song.gp')


def test_check_extension_rejects_unknown():
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension('song.mid')


def test_check_extension_accepts_gp345():
    for ext in ('song.gp3', 'song.gp4', 'song.gp5'):
        pipeline._check_extension(ext)  # must not raise


def test_arrangement_zero_phrase_check_uses_manifest_index():
    manifest = {
        'arrangements': [
            {'id': 'drums', 'type': 'drums', 'drum_tab': 'drum_tab_drums.json'},
            {'id': 'lead', 'file': 'lead.json'},
        ],
    }
    arrangement_files = {
        'lead.json': {
            'notes': [],
            'phrases': [{
                'start_time': 0.0,
                'end_time': 2.0,
                'max_difficulty': 1,
                'levels': [],
            }],
        },
    }

    assert pipeline._manifest_arrangement_zero_has_phrases(manifest, arrangement_files) is False


def test_arrangement_zero_phrase_check_matches_manifest_path_basename():
    manifest = {'arrangements': [{'id': 'lead', 'file': 'arrangements/lead.json'}]}
    arrangement_files = {
        'lead.json': {
            'notes': [],
            'phrases': [{
                'start_time': 0.0,
                'end_time': 2.0,
                'max_difficulty': 1,
                'levels': [],
            }],
        },
    }

    assert pipeline._manifest_arrangement_zero_has_phrases(manifest, arrangement_files) is True


def test_capo_for_track_reads_offset():
    song = pipeline.guitarpro.Song()
    song.tracks[0].offset = 3
    assert pipeline._capo_for_track(song, 0) == 3


def test_capo_for_track_defaults_to_zero_when_none():
    song = pipeline.guitarpro.Song()
    song.tracks[0].offset = None
    assert pipeline._capo_for_track(song, 0) == 0


def test_capo_for_track_out_of_range_returns_zero():
    song = pipeline.guitarpro.Song()
    assert pipeline._capo_for_track(song, 99) == 0


@fixture_available
def test_parse_gp_reads_money_metadata():
    parsed = pipeline.parse_gp(str(MONEY_GP5))
    assert parsed['title'] == 'Money'
    assert parsed['artist'] == 'Pink Floyd'
    assert len(parsed['tracks']) > 0


@fixture_available
def test_build_feedpak_extracts_all_16_sections():
    """Regression test locking in the audit's flagship fix: 'Money (J).gp'
    was documented as losing all 16 of its section markers through the
    legacy sloppak pipeline. The GP5 companion file must not regress."""
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1, 3],
        arrangement_names={1: 'Rhythm', 3: 'Bass'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['arrangement_count'] == 2
    assert result['duration'] > 300  # Money runs ~6:26

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        timeline = json.loads(zf.read('song_timeline.json'))
    assert len(timeline['sections']) == 16
    assert timeline['sections'][0]['name'] == 'soundeffectsintro'
    assert len(timeline['beats']) > 0


@fixture_available
def test_build_feedpak_combines_compatible_same_name_tracks():
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1, 2],
        arrangement_names={1: 'Rhythm', 2: 'Rhythm'},
        combine_same_name=True,
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['arrangement_count'] == 1

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        arrangement = json.loads(zf.read(manifest['arrangements'][0]['file']))
    assert arrangement['notes'] or arrangement['chords']
    assert all(0 <= chord['id'] < len(arrangement['templates'])
               for chord in arrangement['chords'])
    assert all(0 <= shape['chord_id'] < len(arrangement['templates'])
               for shape in arrangement['handshapes'])


@fixture_available
def test_build_feedpak_cover_is_referenced_by_manifest(tmp_path):
    """End-to-end regression test for the cover.jpg-written-but-not-
    pointed-to bug: a cover_path must not just land in the zip, it must
    make the manifest's `cover` key point at it — see
    test_pack.test_assemble_manifest_includes_cover_when_present for the
    unit-level version of this same fix."""
    cover_path = tmp_path / 'art.jpg'
    cover_path.write_bytes(b'\xff\xd8\xff\xe0fake-jpeg-bytes')

    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1],
        arrangement_names={1: 'Rhythm'},
        audio_mode='none',
        cover_path=str(cover_path),
        report=lambda stage, pct: None,
    )

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = zf.namelist()
        manifest = yaml.safe_load(zf.read('manifest.yaml'))

    assert 'cover.jpg' in names
    assert manifest.get('cover') == 'cover.jpg'


@fixture_available
def test_build_feedpak_without_audio_warns_authoring_intermediate():
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert any('authoring intermediate' in w for w in result['warnings'])
    assert 'manifest.yaml' in result['validation']  # empty stems fails minItems


@fixture_available
def test_build_feedpak_feeds_resolved_audio_offset_into_convert_file(monkeypatch, tmp_path):
    """Regression test: audio_offset used to be resolved AFTER convert_file
    ran and was never fed back in — 'embedded'/'sync' mode's real offset
    (GP8 lead-in, or autosync alignment) was silently discarded, since
    there's no separate manifest-level offset field anywhere in this
    pipeline (feedpakr_pack.py has none) for it to land in instead. Locks
    in the fix: whatever _resolve_audio returns must reach convert_file's
    own audio_offset kwarg, the only place that actually shifts note times."""
    audio_path = tmp_path / 'audio.ogg'
    audio_path.write_bytes(b'OggS')
    monkeypatch.setattr(
        pipeline, '_resolve_audio',
        lambda *a, **k: (str(audio_path), 1.75),
    )
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)

    captured = {}
    real_convert_file = pipeline.gp2rs.convert_file

    def _spy_convert_file(*args, **kwargs):
        captured['audio_offset'] = kwargs.get('audio_offset')
        return real_convert_file(*args, **kwargs)

    monkeypatch.setattr(pipeline.gp2rs, 'convert_file', _spy_convert_file)

    pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='sync',
        report=lambda stage, pct: None,
    )
    assert captured['audio_offset'] == 1.75


@fixture_available
def test_build_feedpak_rejects_failed_requested_audio(monkeypatch):
    monkeypatch.setattr(
        pipeline, '_resolve_audio',
        lambda *a, **k: (None, 0.0),
    )
    with pytest.raises(RuntimeError, match='Audio could not be produced'):
        pipeline.build_feedpak(
            str(MONEY_GP5),
            track_indices=[3],
            arrangement_names={3: 'Bass'},
            audio_mode='midi',
            report=lambda stage, pct: None,
        )


# ── GPIF (.gp / .gpx, GP6/7/8) ────────────────────────────────────────────

@fixture_available
def test_build_feedpak_existing_pack_mode_keeps_original_stems_and_cover(tmp_path, monkeypatch):
    """'existing_pack' audio mode: the chart is freshly parsed from GP, but
    audio/stems/cover are reused byte-for-byte from a previously-uploaded
    pack (feedpakr_upgrade.extract_pack_assets' return shape) instead of
    being synthesized/embedded/re-recorded."""
    guitar_path = tmp_path / 'guitar.ogg'
    guitar_path.write_bytes(b'OggS-guitar')
    vocals_path = tmp_path / 'vocals.ogg'
    vocals_path.write_bytes(b'OggS-vocals')
    cover_path = tmp_path / 'cover.jpg'
    cover_path.write_bytes(b'\xff\xd8\xff-jpeg')

    existing_pack = {
        'stems': [
            {'id': 'guitar', 'file': str(guitar_path), 'name': 'Guitar'},
            {'id': 'vocals', 'file': str(vocals_path)},
        ],
        'full_mix_path': None,
        'cover_path': str(cover_path),
        'sync_reference_path': str(guitar_path),  # irrelevant once _resolve_audio is stubbed
    }
    # Autosync itself (chroma-DTW/librosa) is exercised by feedpakr_audio's
    # own tests — stub _resolve_audio here so this test stays about the
    # existing_pack plumbing (extra stems + cover fallback), not autosync.
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (None, 0.0))

    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='existing_pack',
        existing_pack=existing_pack,
        report=lambda stage, pct: None,
    )

    assert result['features']['real_audio'] is True
    assert result['features']['already_separated'] is True
    assert not any('authoring intermediate' in w for w in result['warnings'])

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = set(zf.namelist())
        assert {'stems/guitar.ogg', 'stems/vocals.ogg', 'cover.jpg'} <= names
        assert zf.read('stems/guitar.ogg') == b'OggS-guitar'
        manifest = yaml.safe_load(zf.read('manifest.yaml'))

    stem_ids = {s['id'] for s in manifest['stems']}
    assert stem_ids == {'guitar', 'vocals'}
    guitar_entry = next(s for s in manifest['stems'] if s['id'] == 'guitar')
    assert guitar_entry['name'] == 'Guitar'
    assert manifest['cover'] == 'cover.jpg'


@fixture_available
def test_build_feedpak_existing_pack_sanitizes_stem_ids_for_archive_paths(tmp_path, monkeypatch):
    unsafe_path = tmp_path / 'unsafe.ogg'
    unsafe_path.write_bytes(b'OggS-unsafe')
    existing_pack = {
        'stems': [{'id': '../lead', 'file': str(unsafe_path)}],
        'full_mix_path': None,
        'cover_path': None,
        'sync_reference_path': str(unsafe_path),
    }
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (None, 0.0))

    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='existing_pack',
        existing_pack=existing_pack,
        report=lambda stage, pct: None,
    )

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        stem_file = manifest['stems'][0]['file']
        assert stem_file == 'stems/lead.ogg'
        assert stem_file in zf.namelist()
        assert not any(name.startswith('../') or '/..' in name for name in zf.namelist())


@fixture_available
def test_build_feedpak_existing_pack_mode_with_full_mix_no_extra_stems(tmp_path, monkeypatch):
    """A source pack whose only stem is 'full' (single unseparated mix)
    reuses that mixdown directly — no extra_stems entries, same shape as
    'sync'/'embedded' mode's single-stem output."""
    full_path = tmp_path / 'full.ogg'
    full_path.write_bytes(b'OggS-full')
    existing_pack = {
        'stems': [],
        'full_mix_path': str(full_path),
        'cover_path': None,
        'sync_reference_path': str(full_path),
    }
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (str(full_path), 0.0))
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)

    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='existing_pack',
        existing_pack=existing_pack,
        report=lambda stage, pct: None,
    )
    assert result['features']['already_separated'] is False

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert manifest['stems'] == [{'id': 'full', 'file': 'stems/full.ogg', 'default': True}]


@gp8_fixture_available
def test_parse_gp_gpif_detects_vocal_track():
    parsed = pipeline.parse_gp(str(MONEY_GP8))
    assert parsed['title'] == 'Money'
    vocal_tracks = [t for t in parsed['tracks'] if t['is_vocal']]
    assert len(vocal_tracks) == 1
    assert vocal_tracks[0]['auto_name'] == 'Vocals'


@gp8_fixture_available
def test_build_feedpak_gpif_vocal_track_becomes_lyrics_not_arrangement():
    """The GPIF vocal-track XML has root <vocals>, not <song> — routing it
    through song.parse_arrangement instead of the lyrics path was a real
    bug caught while implementing this (an empty-named ghost arrangement,
    no lyrics.json at all). Locks in the fix."""
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[0, 1, 3],  # Singer (vocal), Rhythm guitar, Bass
        arrangement_names={0: 'Vocals', 1: 'Rhythm', 3: 'Bass'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['arrangement_count'] == 2  # vocal track did NOT become an arrangement

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = zf.namelist()
        assert 'lyrics.json' in names
        lyrics = json.loads(zf.read('lyrics.json'))
        manifest = yaml.safe_load(zf.read('manifest.yaml'))

    assert len(lyrics) > 100  # Money has ~169 sung syllables
    assert lyrics[0]['w'] in ('Mo-', 'Money')  # real lyric, not empty
    assert manifest['lyrics_source'] == 'authored'
    assert manifest['arrangements'][0]['name'] != ''  # no ghost arrangement


@gp8_fixture_available
def test_build_feedpak_gpif_drums_become_type_drums_arrangement():
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[1, 6],  # guitar, drums
        arrangement_names={1: 'Lead', 6: 'Drums'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        drums_entry = next(a for a in manifest['arrangements'] if a['name'] == 'Drums')
        assert drums_entry['type'] == 'drums'
        assert 'file' not in drums_entry
        drum_tab = json.loads(zf.read(drums_entry['drum_tab']))

    assert drum_tab['hits']
    assert drum_tab['kit']
    assert result['features']['drum_arrangements'] == 1
    assert not any(name.startswith('drum_tab_') for name in result['validation'])


@gp8_fixture_available
def test_gpif_capo_lookup_reads_capo_fret():
    """GPIF capo lives at Track/Staves/Staff/Properties/Property[@name=
    'CapoFret']/Fret — neither gp2rs nor gp2rs_gpx read it on their own
    (same class of gap as the GP3-5 capo fix). 'Money (J).gp' itself has
    no capo'd tracks, so this checks the extractor against a fixture that
    does, rather than only asserting an empty dict."""
    capo_path = FIXTURE_DIR / 'Three Days Grace-Animal I Have Become (Standard Tuning)-10-03-2024.gp'
    if not capo_path.is_file():
        pytest.skip('capo sample fixture not present on this machine')
    capo = pipeline._gpif_capo_lookup(str(capo_path))
    assert capo.get(6) == 2


# ── Phase 3 fidelity: keys.json, handshapes, drums-as-arrangements,
#    notation, vocal_pitch ─────────────────────────────────────────────────

@fixture_available
def test_build_feedpak_gp345_drums_become_type_drums_arrangement():
    """A GP3-5 drum track gets a real drum_tab.json + `type: drums` entry
    (spec 1.17.0) instead of the legacy string*24+fret fretted encoding —
    gp2rs_gpx has no equivalent, so this is GP3-5 only."""
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1, 6],  # Rhythm guitar, Drums
        arrangement_names={1: 'Rhythm', 6: 'Drums'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    assert result['arrangement_count'] == 2

    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = zf.namelist()
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        drums_entry = next(a for a in manifest['arrangements'] if a['name'] == 'Drums')
        assert drums_entry['type'] == 'drums'
        assert 'file' not in drums_entry
        drum_tab = json.loads(zf.read(drums_entry['drum_tab']))

    assert drum_tab['hits']  # real hit data, not empty
    assert drum_tab['kit']


@fixture_available
def test_build_feedpak_handshapes_derived_from_chords():
    """gp2rs never populates handShapes ('empty for now') — feedpakr derives
    them from the chord data every source has. Money's rhythm guitar has
    real chord hits, so this should produce a non-empty list."""
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1],
        arrangement_names={1: 'Rhythm'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        rhythm = json.loads(zf.read('arrangements/rhythm.json'))
    assert rhythm['handshapes']
    first = rhythm['handshapes'][0]
    assert set(first) == {'chord_id', 'start_time', 'end_time', 'arp'}
    assert first['end_time'] > first['start_time']


@gp8_fixture_available
def test_build_feedpak_gpif_keys_track_gets_notation():
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[3, 5],  # Bass, Electric Piano (keys)
        arrangement_names={3: 'Bass', 5: 'Keys'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = zf.namelist()
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        keys_entry = next(a for a in manifest['arrangements'] if a['name'] == 'Keys')
        assert 'notation' in keys_entry
        notation = json.loads(zf.read(keys_entry['notation']))

    assert notation['instrument'] == 'piano'
    assert notation['measures']


@gp8_fixture_available
def test_build_feedpak_gpif_non_keys_track_can_request_notation():
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[3],  # Bass: melodic, but not auto-enabled like keys.
        arrangement_names={3: 'Bass'},
        notation_track_indices={3},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        entry = manifest['arrangements'][0]
        notation = json.loads(zf.read(entry['notation']))
    assert notation['instrument'] == 'melodic'
    assert notation['measures']


@gp8_fixture_available
def test_build_feedpak_gpif_notation_selection_can_disable_keys_default():
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[5],
        arrangement_names={5: 'Keys'},
        notation_track_indices=set(),
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    assert 'notation' not in manifest['arrangements'][0]


@fixture_available
def test_build_feedpak_reports_only_recorded_audio_as_split_eligible(tmp_path, monkeypatch):
    audio_path = tmp_path / 'recording.ogg'
    audio_path.write_bytes(b'OggS')
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (str(audio_path), 0.0))
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='sync',
        report=lambda stage, pct: None,
    )
    assert result['features']['real_audio'] is True


@gp8_fixture_available
def test_build_feedpak_gpif_vocal_track_produces_vocal_pitch():
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[0, 3],  # Singer (vocal), Bass
        arrangement_names={0: 'Vocals', 3: 'Bass'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    import io
    import json
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        names = zf.namelist()
        assert 'vocal_pitch.json' in names
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
        assert manifest['vocal_pitch'] == 'vocal_pitch.json'
        vp = json.loads(zf.read('vocal_pitch.json'))

    assert vp['version'] == 1
    assert len(vp['notes']) > 100
    assert all(0 < n['midi'] <= 127 for n in vp['notes'])


@fixture_available
def test_build_feedpak_gp345_keys_json_present_when_extractable():
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1],
        arrangement_names={1: 'Rhythm'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        manifest = yaml.safe_load(zf.read('manifest.yaml'))
    # Whatever key the source declares (or its default), the pointer and
    # side file must be consistently present together.
    assert manifest.get('keys') == 'keys.json'
