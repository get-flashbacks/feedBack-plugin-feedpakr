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
    if 'ffmpeg' in required and not _support.ffmpeg_available():
        missing.append('ffmpeg not on PATH')
    if missing:
        pytest.exit(
            'required capability missing (via ' + _support.REQUIRE_ENV + '): '
            + '; '.join(missing),
            returncode=1,
        )


_skipped: list[tuple[str, str]] = []


def pytest_runtest_logreport(report):
    # Record every skip so pytest_sessionfinish can refuse the ones that a
    # required-capability run shouldn't tolerate (e.g. ffmpeg present but
    # lacking the lavfi/vorbis filters the mixdown tests need, or a host
    # sub-module the coarse 3-symbol check above didn't probe).
    if report.skipped:
        longrepr = report.longrepr
        if isinstance(longrepr, tuple) and len(longrepr) >= 3:
            reason = str(longrepr[2])
        else:
            reason = str(longrepr)
        _skipped.append((report.nodeid, reason))


def _skip_tolerated(reason: str, required: set[str]) -> bool:
    if any(token in reason for token in _support.OPTIONAL_SKIP_SUBSTRINGS):
        return True
    for substr, capability in _support.CAPABILITY_SKIP_SUBSTRINGS.items():
        if substr in reason:
            return capability is not None and capability not in required
    return False  # unrecognized skip — surface it for review


def pytest_sessionfinish(session, exitstatus):
    """Turn any non-allowlisted skip into a failure when capabilities are
    required, so a skipped regression can never look like a green CI run."""
    required = _support.required_capabilities()
    if not required:
        return
    unexpected = [
        (node, reason) for node, reason in _skipped
        if not _skip_tolerated(reason, required)
    ]
    if not unexpected:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    terminalreporter = session.config.pluginmanager.get_plugin('terminalreporter')
    if terminalreporter is not None:
        terminalreporter.write_sep(
            '=', 'ERROR: unexpected skips in a required-capability run',
        )
        terminalreporter.write_line(
            _support.REQUIRE_ENV + ' is set, but these tests skipped for reasons not '
            'tolerated for the required capabilities — provision the capability or '
            'update _support.CAPABILITY_SKIP_SUBSTRINGS / OPTIONAL_SKIP_SUBSTRINGS:',
        )
        for node, reason in unexpected:
            terminalreporter.write_line(f'  {node}: {reason}')
