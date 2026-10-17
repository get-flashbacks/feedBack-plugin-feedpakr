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

Kept import-light (``os``/``shutil``/``pathlib`` only) so `conftest.py` and
individual test modules can import it without pulling in the host lib.
"""

from __future__ import annotations

import os
import shutil
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


def local_fixture(*parts: str) -> Path | None:
    """Return ``local_fixtures_dir()/parts`` when the root exists, else ``None``."""
    root = local_fixtures_dir()
    if root is None:
        return None
    return root.joinpath(*parts)


def ffmpeg_available() -> bool:
    """True when an ``ffmpeg`` executable is on ``PATH``."""
    return shutil.which('ffmpeg') is not None


def required_capabilities() -> set[str]:
    """Parse ``FEEDPAKR_REQUIRE`` into a set of lowercase capability names."""
    raw = os.environ.get(REQUIRE_ENV, '')
    return {token.strip().lower() for token in raw.split(',') if token.strip()}
