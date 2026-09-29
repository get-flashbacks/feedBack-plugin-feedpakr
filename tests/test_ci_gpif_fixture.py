"""CI-owned end-to-end regression coverage for a small GP7/8 import.

The fixture is deliberately authored for this repository, not copied from a
song tab. It keeps the actual ZIP/GPIF loader and conversion path exercised
on every CI run, while the larger developer-local real-tab suite remains an
optional exploratory check.

Its values are chosen to be load-bearing rather than defaults: the low
string is detuned to D and a <MasterTrack> carries 80 BPM, so the tuning and
beat-time assertions each fail if the corresponding read regresses to a
fallback.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

import feedpakr_pipeline as pipeline


FIXTURE = Path(__file__).parent / 'fixtures' / 'ci-minimal.gp'
HOST_AVAILABLE = (
    pipeline.guitarpro is not None
    and pipeline.gp2rs is not None
    and pipeline.gp2rs_gpx is not None
)

pytestmark = pytest.mark.skipif(
    not HOST_AVAILABLE, reason='feedBack host core lib not on sys.path'
)


def test_ci_gpif_fixture_imports_through_the_full_pipeline():
    """The committed GPIF container must parse and produce playable notes."""
    parsed = pipeline.parse_gp(str(FIXTURE))

    assert parsed['format'] == 'gpif'
    assert parsed['title'] == 'CI Fixture'
    assert parsed['tempo'] == 80.0
    assert [(track['index'], track['auto_name']) for track in parsed['tracks']] == [
        (0, 'Lead')
    ]

    result = pipeline.build_feedpak(
        str(FIXTURE),
        track_indices=[0],
        arrangement_names={0: 'Lead'},
        audio_mode='none',
    )

    assert result['arrangement_count'] == 1
    # Read the arrangement through the manifest so its `file` pointer and the
    # written member are pinned to each other — the same manifest-resolved
    # read tests/test_pipeline.py:555 does for the drum tab.
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as archive:
        manifest = yaml.safe_load(archive.read('manifest.yaml'))
        arrangement = json.loads(archive.read(manifest['arrangements'][0]['file']))

    # audio_mode='none' leaves stems: [] — the feedpak spec §5.3.2
    # authoring-intermediate carve-out, surfaced as a warning rather than a
    # defect. Any OTHER entry means the pack is internally inconsistent.
    assert result['validation'] == {
        'manifest.yaml': ['stems: [] should be non-empty'],
    }

    # Detuned low string: offsets from standard, so the no-tuning fallback
    # ([0] * 6) and standard EADGBE both fail this line.
    assert arrangement['tuning'] == [-2, 0, 0, 0, 0, 0]
    # The second beat lands at 60/80s. Exactly representable in binary and
    # not a multiple of 0.5, so the asserted time can only come from the
    # <MasterTrack> BPM, not from _gpif_tempo's 120.0 default.
    assert [(note['s'], note['f'], note['t']) for note in arrangement['notes']] == [
        (0, 0, 0.0),
        (5, 3, 0.75),
    ]
