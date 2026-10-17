import shutil
import sys
import importlib
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HOST_LIB_DIR = PLUGIN_DIR.parent / 'pakr' / 'feedBack' / 'lib'

# feedpakr's siblings are loaded via context['load_sibling'] at runtime
# (namespaced module names) — for tests we just need them importable.
sys.path.insert(0, str(PLUGIN_DIR))

# feedpakr_pipeline needs the host's core lib (guitarpro, gp2rs, song,
# gp2midi) on sys.path. Point at the sibling checkout used during
# development; skip silently (tests that need it will self-skip) if it
# isn't there — e.g. in an environment that only has this one repo.
if HOST_LIB_DIR.is_dir():
    sys.path.insert(0, str(HOST_LIB_DIR))


import feedpakr_pipeline
import _support

feedpakr_pipeline.configure_sibling_loader(importlib.import_module)

HOST_AVAILABLE = (
    feedpakr_pipeline.guitarpro is not None
    and feedpakr_pipeline.gp2rs is not None
    and feedpakr_pipeline.gp2rs_gpx is not None
)


def pytest_configure(config):
    """Fail loudly when CI requires a capability this host is missing.

    Tests self-skip when the host lib or ``ffmpeg`` is unavailable, which is
    right for a plugin-only local checkout. But that same self-skip would
    quietly hide a broken CI job, so ``FEEDPAKR_REQUIRE`` (set in
    ``.github/workflows/tests.yml``) turns the missing capability into a hard
    error before collection. Unset locally, this is a no-op.
    """
    required = _support.required_capabilities()
    missing = []
    if 'host' in required and not HOST_AVAILABLE:
        missing.append('host core lib (guitarpro/gp2rs/gp2rs_gpx) not importable')
    if 'ffmpeg' in required and shutil.which('ffmpeg') is None:
        missing.append('ffmpeg not on PATH')
    if missing:
        pytest.exit(
            'required capability missing (via ' + _support.REQUIRE_ENV + '): '
            + '; '.join(missing),
            returncode=1,
        )
