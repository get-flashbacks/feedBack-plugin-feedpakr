"""CI-owned end-to-end regression coverage for a small GP7/8 import.

The fixture is deliberately authored for this repository, not copied from a
song tab. It keeps the actual ZIP/GPIF loader and conversion path exercised
on every CI run, while the larger developer-local real-tab suite remains an
optional exploratory check.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

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
    with zipfile.ZipFile(io.BytesIO(result['bytes'])) as archive:
        arrangement = json.loads(archive.read('arrangements/lead.json'))

    assert arrangement['tuning'] == [0, 0, 0, 0, 0, 0]
    assert [(note['s'], note['f'], note['t']) for note in arrangement['notes']] == [
        (0, 0, 0.0),
        (5, 3, 0.5),
    ]
