"""Tests for feedpakr_pipeline.py. Needs the feedBack host core lib
(guitarpro, gp2rs, song, gp2midi) on sys.path — see conftest.py — and,
for the fixture-backed tests, the c:\\Users\\PC\\Downloads\\Alll sample
folder. Both self-skip cleanly when unavailable rather than false-failing
in an environment that only has this one repo checked out.
"""

import types
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


def test_check_extension_accepts_gpx_and_gp(tmp_path):
    # _check_extension now reads the file's magic bytes (decompression-bomb
    # guard, feedpakr#37) as part of the extension check, so it needs a real
    # file on disk rather than a bare placeholder path. .gpx (GP6) is a
    # BCFZ-magic container; .gp (GP7/8) is a zip — an empty/non-PK file for
    # .gp is enough to hit the guard's "not a zip container, pass through"
    # branch, matching test_gpx_safety.py's fixture conventions.
    gpx_path = tmp_path / 'song.gpx'
    gpx_path.write_bytes(b'BCFZ')
    pipeline._check_extension(str(gpx_path))  # must not raise

    gp_path = tmp_path / 'song.gp'
    gp_path.write_bytes(b'')
    pipeline._check_extension(str(gp_path))


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
        lambda *a, **k: (str(audio_path), 1.75, []),
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
        lambda *a, **k: (None, 0.0, []),
    )
    with pytest.raises(RuntimeError, match='Audio could not be produced'):
        pipeline.build_feedpak(
            str(MONEY_GP5),
            track_indices=[3],
            arrangement_names={3: 'Bass'},
            audio_mode='midi',
            report=lambda stage, pct: None,
        )


# ── Tempo-aware warp (_build_warp_fn / _warp_arrangement) ──────────────────
#
# These don't need the MONEY_GP5 fixture (unlike almost everything else in
# this file) — bar_start_times/build_warp_anchors/warp_time/
# gp_has_expandable_repeats are librosa-free and pure/gp_path-driven, so
# stubbing pipeline.gp_autosync.bar_start_times keeps them fixture-free and
# runnable wherever the host lib is on sys.path (HOST_AVAILABLE), i.e. in CI.

GP_AUTOSYNC_AVAILABLE = pipeline.gp_autosync is not None
autosync_available = pytest.mark.skipif(
    not GP_AUTOSYNC_AVAILABLE, reason='gp_autosync not on sys.path'
)


@autosync_available
def test_build_warp_fn_builds_a_working_piecewise_warp(monkeypatch):
    """Happy path: enough sync points produce a real warp_fn, no warnings."""
    monkeypatch.setattr(
        pipeline.gp_autosync, 'bar_start_times',
        lambda gp_path: [0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
    )
    sync_points = [
        {'bar': 0, 'time_secs': 0.5, 'modified_bpm': 120.0},
        {'bar': 3, 'time_secs': 7.0, 'modified_bpm': 120.0},
        {'bar': 5, 'time_secs': 11.5, 'modified_bpm': 118.0},
    ]
    warnings: list[str] = []

    warp_fn = pipeline._build_warp_fn('fake.gp5', sync_points, warnings)

    assert warp_fn is not None
    assert warnings == []
    # Anchors land exactly on the sampled points; between/around them the
    # mapping should be monotonically increasing (a real warp, not a no-op).
    assert warp_fn(0.0) == pytest.approx(0.5)
    assert warp_fn(6.0) == pytest.approx(7.0)
    assert warp_fn(10.0) == pytest.approx(11.5)
    assert warp_fn(0.0) < warp_fn(6.0) < warp_fn(10.0)


@autosync_available
def test_build_warp_fn_degrades_to_none_with_too_few_anchors(monkeypatch):
    """A single sync point can't build a >=2-point anchor list — falls back
    to the flat offset (None) with an explanatory warning, not an error."""
    monkeypatch.setattr(
        pipeline.gp_autosync, 'bar_start_times',
        lambda gp_path: [0.0, 2.0, 4.0],
    )
    warnings: list[str] = []

    warp_fn = pipeline._build_warp_fn('fake.gp5', [{'bar': 0, 'time_secs': 0.5}], warnings)

    assert warp_fn is None
    assert len(warnings) == 1
    assert 'constant offset' in warnings[0]


@autosync_available
def test_build_warp_fn_degrades_to_none_on_bar_start_times_failure(monkeypatch):
    """bar_start_times raising (unparseable gp_path, etc.) must degrade to
    the flat offset, not propagate — this runs inside a best-effort stage
    of build_feedpak."""
    def _boom(gp_path):
        raise ValueError('cannot parse')
    monkeypatch.setattr(pipeline.gp_autosync, 'bar_start_times', _boom)
    warnings: list[str] = []

    warp_fn = pipeline._build_warp_fn('fake.gp5', [{'bar': 0, 'time_secs': 0.5}], warnings)

    assert warp_fn is None
    assert len(warnings) == 1
    assert 'cannot parse' in warnings[0]


def test_build_warp_fn_silent_none_with_no_sync_points():
    """Manual offset / non-autosync modes pass sync_points=[] — that's the
    expected 'no warp available' case, not a degraded one, so no warning
    should be emitted (would otherwise spam every non-autosync build)."""
    warnings: list[str] = []
    assert pipeline._build_warp_fn('fake.gp5', [], warnings) is None
    assert warnings == []


@pytest.mark.skipif(pipeline.song_mod is None, reason='song module not on sys.path')
@autosync_available
def test_warp_arrangement_shifts_notes_and_anchors_preserving_duration():
    song_mod = pipeline.song_mod
    arr = song_mod.Arrangement(name='Test')
    held_note = song_mod.Note(time=0.0, string=0, fret=0, sustain=2.0)
    plain_note = song_mod.Note(time=8.0, string=1, fret=2, sustain=0.0)
    anchor = song_mod.Anchor(time=4.0, fret=1, width=4)
    arr.notes = [held_note, plain_note]
    arr.anchors = [anchor]

    def warp(t):
        return t * 2.0 + 1.0  # distinguishable affine warp

    pipeline._warp_arrangement(arr, warp)

    assert held_note.time == pytest.approx(1.0)          # warp(0.0)
    assert held_note.sustain == pytest.approx(4.0)        # warp(2.0) - warp(0.0)
    assert plain_note.time == pytest.approx(17.0)         # warp(8.0)
    assert plain_note.sustain == pytest.approx(0.0)
    assert anchor.time == pytest.approx(9.0)              # warp(4.0)


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
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (None, 0.0, []))

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
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (None, 0.0, []))

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
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (str(full_path), 0.0, []))
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


# ── Tempo-aware warp, manual offset, repeat gate: end-to-end ───────────────
# Needs MONEY_GP5 (real guitarpro.parse + gp2rs.convert_file), unlike the
# fixture-free _build_warp_fn/_warp_arrangement tests above.

@fixture_available
def test_build_feedpak_tempo_aware_warp_differs_from_flat_offset(monkeypatch, tmp_path):
    """A genuine multi-point (tempo-stretched) sync_points list must produce
    output that's actually warped per-bar, not just a relabeled flat shift.
    Compares two builds of the same file: one with 3 sync points describing
    a 15% tempo stretch, one with just the first of those points (forcing
    the pre-existing flat-offset fallback since _build_warp_fn needs >=2).
    Both are anchored at the same bar-0 offset, so the builds should agree
    near the start and diverge by the end — that divergence is the whole
    point of this feature."""
    audio_path = tmp_path / 'audio.ogg'
    audio_path.write_bytes(b'OggS')
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)

    bar_starts = pipeline.gp_autosync.bar_start_times(str(MONEY_GP5))
    assert len(bar_starts) >= 6, 'fixture too short to sample a spread of bars for this test'
    last_bar = len(bar_starts) - 1

    def synth_time(bar):
        return bar_starts[bar] * 1.15 + 0.3

    full_points = [
        {'bar': 0, 'time_secs': synth_time(0), 'modified_bpm': 120.0},
        {'bar': last_bar // 2, 'time_secs': synth_time(last_bar // 2), 'modified_bpm': 120.0},
        {'bar': last_bar, 'time_secs': synth_time(last_bar), 'modified_bpm': 120.0},
    ]

    real_convert_file = pipeline.gp2rs.convert_file
    captured_offsets = []

    def _spy_convert_file(*args, **kwargs):
        captured_offsets.append(kwargs.get('audio_offset'))
        return real_convert_file(*args, **kwargs)

    def _build(sync_points):
        monkeypatch.setattr(
            pipeline, '_resolve_audio',
            lambda *a, **k: (str(audio_path), synth_time(0), sync_points),
        )
        monkeypatch.setattr(pipeline.gp2rs, 'convert_file', _spy_convert_file)
        return pipeline.build_feedpak(
            str(MONEY_GP5),
            track_indices=[3],
            arrangement_names={3: 'Bass'},
            audio_mode='sync',
            user_audio_path=str(audio_path),
            report=lambda stage, pct: None,
        )

    warped = _build(full_points)
    flat = _build([full_points[0]])  # 1 point -> _build_warp_fn degrades to None

    assert warped['features']['tempo_aware_sync'] is True
    assert flat['features']['tempo_aware_sync'] is False
    # convert_file got 0.0 for the warped build (correction applied to the
    # parsed Arrangement/Song afterward instead), and the real scalar for
    # the flat-fallback build.
    assert captured_offsets[0] == 0.0
    assert captured_offsets[1] == pytest.approx(synth_time(0))

    def _note_and_chord_times(result):
        import io, zipfile, json
        with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
            manifest = yaml.safe_load(zf.read('manifest.yaml'))
            arr_file = next(
                (a['file'] for a in manifest['arrangements'] if a['name'] == 'Bass'), None,
            )
            assert arr_file is not None, 'build produced no "Bass" arrangement'
            wire = json.loads(zf.read(arr_file))
        return sorted(
            [n['t'] for n in wire.get('notes', [])] + [c['t'] for c in wire.get('chords', [])]
        )

    warped_times = _note_and_chord_times(warped)
    flat_times = _note_and_chord_times(flat)
    assert warped_times and flat_times

    # Both anchor at bar 0, so the earliest events should be close...
    assert warped_times[0] == pytest.approx(flat_times[0], abs=0.05)
    # ...but a real 15% tempo stretch across the whole song must diverge
    # from a flat shift by more than rounding noise by the end.
    assert abs(warped_times[-1] - flat_times[-1]) > 0.05


@fixture_available
def test_build_feedpak_manual_offset_bypasses_autosync(monkeypatch, tmp_path):
    """manual_offset must skip autosync entirely (even though nothing here
    would make it fail) and disable tempo-aware sync, using the given flat
    offset instead — the opt-out for when autosync gets it wrong."""
    audio_path = tmp_path / 'audio.ogg'
    audio_path.write_bytes(b'OggS')
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)

    def _autosync_should_not_run(*args, **kwargs):
        raise AssertionError('autosync_audio must not run when manual_offset is set')
    monkeypatch.setattr(pipeline.audio_mod, 'autosync_audio', _autosync_should_not_run)

    real_convert_file = pipeline.gp2rs.convert_file
    captured = {}

    def _spy_convert_file(*args, **kwargs):
        captured['audio_offset'] = kwargs.get('audio_offset')
        return real_convert_file(*args, **kwargs)
    monkeypatch.setattr(pipeline.gp2rs, 'convert_file', _spy_convert_file)

    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='sync',
        user_audio_path=str(audio_path),
        manual_offset=2.5,
        report=lambda stage, pct: None,
    )

    assert result['features']['tempo_aware_sync'] is False
    assert captured['audio_offset'] == 2.5
    assert not any('Autosync' in w for w in result['warnings'])


@fixture_available
def test_build_feedpak_gp345_repeats_disable_warp(monkeypatch, tmp_path):
    """A GP3-5 file with expandable repeats must not get the per-bar warp
    (auto_sync's anchors are as-written; convert_file's default
    expand_repeats=True is as-performed — mixing them mis-maps every repeat
    pass past the first) even when autosync produced enough sync points to
    otherwise build one. Falls back to the flat offset with a warning."""
    audio_path = tmp_path / 'audio.ogg'
    audio_path.write_bytes(b'OggS')
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)
    monkeypatch.setattr(pipeline.gp_autosync, 'gp_has_expandable_repeats', lambda gp_path: True)

    sync_points = [
        {'bar': 0, 'time_secs': 0.3, 'modified_bpm': 120.0},
        {'bar': 4, 'time_secs': 8.0, 'modified_bpm': 120.0},
    ]
    monkeypatch.setattr(
        pipeline, '_resolve_audio',
        lambda *a, **k: (str(audio_path), 0.3, sync_points),
    )

    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[3],
        arrangement_names={3: 'Bass'},
        audio_mode='sync',
        user_audio_path=str(audio_path),
        report=lambda stage, pct: None,
    )

    assert result['features']['tempo_aware_sync'] is False
    assert any('repeats' in w and 'Tempo-aware sync disabled' in w for w in result['warnings'])


@gp8_fixture_available
def test_build_feedpak_gpif_vocals_warped_when_tempo_aware_sync_active(monkeypatch, tmp_path):
    """Regression test (pullfrog PR #45 review): the is_vocals_xml branch
    reads lyrics/vocal-pitch timing straight off xml_path, which
    convert_file writes at audio_offset=0.0 when a warp is active — that
    branch continues before reaching the fretted-track warp code, so
    without its own warp pass GPIF lyrics/vocal-pitch would silently keep
    as-written (unwarped) timestamps while every other arrangement in the
    same build was corrected.

    Asserts against an independently-computed expected value (ground truth
    taken from the actual as-written XML, warped with a warp function built
    the same way _build_warp_fn does) rather than a loose tolerance band —
    an earlier version of this test used a tolerance wide enough that an
    un-warped first syllable could pass it too (pullfrog PR #45 follow-up
    review), so it wouldn't actually have failed if the warp pass were
    removed. This one would."""
    audio_path = tmp_path / 'audio.ogg'
    audio_path.write_bytes(b'OggS')
    monkeypatch.setattr(pipeline.audio_mod, 'get_audio_duration', lambda _path: None)

    bar_starts = pipeline.gp_autosync.bar_start_times(str(MONEY_GP8))
    assert len(bar_starts) >= 4, 'fixture too short to sample a spread of bars for this test'
    last_bar = len(bar_starts) - 1

    def synth_time(bar):
        return bar_starts[bar] * 1.2 + 0.5  # a real stretch, not just an offset

    sync_points = [
        {'bar': 0, 'time_secs': synth_time(0), 'modified_bpm': 120.0},
        {'bar': last_bar, 'time_secs': synth_time(last_bar), 'modified_bpm': 120.0},
    ]

    parsed = pipeline.parse_gp(str(MONEY_GP8))
    vocal_indices = [t['index'] for t in parsed['tracks'] if t['is_vocal']]
    assert vocal_indices, 'fixture has no vocal track to exercise this regression against'
    arrangement_names = {vocal_indices[0]: 'Vocals'}

    # Ground truth: convert the vocal track independently at audio_offset=0.0
    # (exactly what build_feedpak does internally once warp_fn is active) and
    # read the LAST syllable's as-written raw time straight off the XML —
    # the value the pipeline's own vocals-warp loop is responsible for
    # warping. Using the last (not first) syllable matters: with only two
    # anchor points, a syllable near bar 0 warps to nearly the same place
    # whether or not the warp actually ran, so only a late syllable — where
    # the 1.2x stretch has accumulated real drift — can tell the two apart.
    raw_xml_paths = pipeline.gp2rs.convert_file(
        str(MONEY_GP8), str(tmp_path / 'raw_xml'),
        track_indices=vocal_indices, arrangement_names=arrangement_names,
        audio_offset=0.0,
    )
    vocals_xml = next((p for p in raw_xml_paths if pipeline.lyrics_mod.is_vocals_xml(p)), None)
    assert vocals_xml is not None, 'fixture vocal track did not produce a <vocals> XML'
    raw_entries = pipeline.lyrics_mod.parse_vocals_xml(vocals_xml)
    assert raw_entries, 'fixture vocal track has no lyric syllables to test against'
    raw_last_t = raw_entries[-1]['t']

    anchors = pipeline.gp_autosync.build_warp_anchors(
        [types.SimpleNamespace(bar=p['bar'], time_secs=p['time_secs']) for p in sync_points],
        bar_starts,
    )
    assert anchors, 'crafted sync_points did not produce usable warp anchors'
    expected_last_t = pipeline.gp_autosync.warp_time(raw_last_t, anchors)
    assert abs(expected_last_t - raw_last_t) > 0.5, (
        'fixture vocal track has no late-song syllable to distinguish warped '
        'from un-warped output against — pick a track/fixture with more spread'
    )

    monkeypatch.setattr(
        pipeline, '_resolve_audio',
        lambda *a, **k: (str(audio_path), synth_time(0), sync_points),
    )

    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=vocal_indices,
        arrangement_names=arrangement_names,
        audio_mode='sync',
        user_audio_path=str(audio_path),
        report=lambda stage, pct: None,
    )

    assert result['features']['tempo_aware_sync'] is True
    assert result['features']['lyrics'] is True

    import io, zipfile, json
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as zf:
        lyrics = json.loads(zf.read('lyrics.json'))
    assert lyrics
    assert lyrics[-1]['t'] == pytest.approx(expected_last_t, abs=0.05)


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
    monkeypatch.setattr(pipeline, '_resolve_audio', lambda *a, **k: (str(audio_path), 0.0, []))
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


@fixture_available
def test_build_feedpak_gp345_lyrics_are_marked_approximate_when_present():
    """A GP3-5 source has no per-syllable timing (song.lyrics is one line
    per measure), so any lyrics this pipeline extracted for it MUST have
    come from the extract_gp345_lyrics() fallback — there's no other lyrics
    path for a .gp5 file. features['lyrics_approximate'] gates the
    lyrics_sync handoff button in screen.js; it must never be True for a
    GPIF source (exact timing, nothing to re-sync)."""
    result = pipeline.build_feedpak(
        str(MONEY_GP5),
        track_indices=[1],
        arrangement_names={1: 'Rhythm'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    features = result['features']
    assert 'lyrics_approximate' in features
    if features['lyrics']:
        assert features['lyrics_approximate'] is True
    else:
        assert features['lyrics_approximate'] is False


@gp8_fixture_available
def test_build_feedpak_gpif_lyrics_are_never_marked_approximate():
    """GPIF sources get exact per-syllable timing from the <vocals> XML —
    features['lyrics_approximate'] must stay False even when lyrics are
    present, so the lyrics_sync handoff button never offers to 're-sync'
    a chart that already has real timing. Includes the vocal track (index
    0, per test_build_feedpak_gpif_vocal_track_becomes_lyrics_not_arrangement)
    so this actually exercises the lyrics-present case, not a no-lyrics one."""
    result = pipeline.build_feedpak(
        str(MONEY_GP8),
        track_indices=[0, 1],  # Singer (vocal), Rhythm guitar
        arrangement_names={0: 'Vocals', 1: 'Rhythm'},
        audio_mode='none',
        report=lambda stage, pct: None,
    )
    features = result['features']
    assert features['lyrics'] is True
    assert features['lyrics_approximate'] is False
