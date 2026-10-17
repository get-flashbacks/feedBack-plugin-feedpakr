"""Shared test-support helpers.

Two kinds of configuration live here:

* **Optional local fixtures** — the developer's personal sample library
  (real Guitar Pro tabs, sample sloppaks). These are never required and are
  never committed; tests that use them self-skip with a reason naming
  ``FEEDPAKR_LOCAL_FIXTURES`` so a checkout without them still runs the
  portable suite. Set ``FEEDPAKR_LOCAL_FIXTURES=/path/to/samples`` to point
  at a different location than the historical default.
* **Required capabilities** — the host core lib and the `ffmpeg` binary.
  CI sets ``FEEDPAKR_REQUIRE=host,ffmpeg`` (see ``.github/workflows/tests.yml``)
  so a missing capability fails the run loudly instead of letting the
  essential regressions skip silently. ``tests/conftest.py`` enforces this.

Kept import-light (``os``/``pathlib`` only) so `conftest.py` and individual
test modules can import it without pulling in the host lib.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Env var pointing at the optional personal sample library.
LOCAL_FIXTURES_ENV = 'FEEDPAKR_LOCAL_FIXTURES'

#: Comma-separated capability list CI requires, e.g. ``host,ffmpeg``.
REQUIRE_ENV = 'FEEDPAKR_REQUIRE'

#: Historical developer-local sample folder, used only as a convenience
#: default when ``FEEDPAKR_LOCAL_FIXTURES`` is unset.
DEFAULT_LOCAL_FIXTURES = Path(r'c:\Users\PC\Downloads\Alll')

#: Shared, greppable skip reason for tests backed by personal samples.
LOCAL_FIXTURE_SKIP_REASON = (
    'optional personal sample library not configured '
    f'(set {LOCAL_FIXTURES_ENV}=/path/to/samples)'
)

#: Substrings of skip reasons that are always acceptable, even in a
#: ``FEEDPAKR_REQUIRE`` run: the missing thing is genuinely optional, or the
#: test's own subject can't be constructed. ``tests/conftest.py`` fails the
#: session on any other skip once a capability is required, so a required
#: regression can't hide as a green skip.
OPTIONAL_SKIP_SUBSTRINGS = (
    LOCAL_FIXTURE_SKIP_REASON,
    'not a known-affected fixture',
    'synthetic pyguitarpro Song has no attachable beats',
)

#: Skip-reason substring -> the capability (as named in ``FEEDPAKR_REQUIRE``)
#: whose absence the skip signals. Such a skip is tolerated only while that
#: capability is *not* required; ``None`` means the skip is never tolerated in
#: a required-capability run. A skip matching neither this map nor
#: :data:`OPTIONAL_SKIP_SUBSTRINGS` is treated as unexpected and fails the run.
CAPABILITY_SKIP_SUBSTRINGS = {
    'feedBack host core lib not on sys.path': 'host',
    'song module not on sys.path': 'host',
    'gp_autosync not on sys.path': 'host',
    'pyguitarpro not installed': 'host',
    'jsonschema not installed': None,
    'ffmpeg not installed': 'ffmpeg',
    'ffmpeg not available': 'ffmpeg',
    'ffmpeg cannot generate a test tone': 'ffmpeg',
}


def local_fixtures_dir() -> Path | None:
    """Resolve the optional sample-library root, or ``None`` if absent.

    ``FEEDPAKR_LOCAL_FIXTURES`` wins when set; otherwise the historical
    default is used if it happens to exist. A configured-but-missing path
    resolves to ``None`` (the dependent tests skip with a reason that names
    the env var, rather than erroring on a bad path).
    """
    raw = os.environ.get(LOCAL_FIXTURES_ENV)
    if raw:
        candidate = Path(raw)
        return candidate if candidate.is_dir() else None
    if DEFAULT_LOCAL_FIXTURES.is_dir():
        return DEFAULT_LOCAL_FIXTURES
    return None


def local_fixtures_root() -> Path:
    """Like :func:`local_fixtures_dir`, but always returns a ``Path``.

    Falls back to :data:`DEFAULT_LOCAL_FIXTURES` when nothing is configured,
    so callers can keep using ``path.is_file()`` guards without a ``None``
    check (the path simply won't exist).
    """
    return local_fixtures_dir() or DEFAULT_LOCAL_FIXTURES


def required_capabilities() -> set[str]:
    """Parse ``FEEDPAKR_REQUIRE`` into a set of lowercase capability names."""
    raw = os.environ.get(REQUIRE_ENV, '')
    return {token.strip().lower() for token in raw.split(',') if token.strip()}
