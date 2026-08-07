"""Hardened XML parsing shared by feedpakr_lyrics.py and feedpakr_tones.py
(security audit: XXE / entity-expansion in xml.etree.ElementTree.parse).

Both callers parse arrangement XML that gp2rs_gpx.convert_file wrote from
an uploaded, untrusted GP/GPX source — a crafted file could carry a DOCTYPE
with external entities or nested entity expansion ("billion laughs").
xml.etree.ElementTree resolves both; defusedxml.ElementTree hardens
parsing against them when installed.

`defusedxml>=0.7.1` is a declared requirements.txt dependency of this
plugin specifically to close this vulnerability, so this module fails
CLOSED rather than open: if it's somehow missing at runtime, safe_parse()
raises instead of silently parsing untrusted XML with unhardened stdlib
ET — a missing security dependency must be a loud failure, not a silent
reintroduction of the exact bug this module exists to fix (review
feedback on #39; lib/gp8_audio_sync.py / lib/gp_autosync.py in the
feedBack host predate that dependency being guaranteed and still fail
open — this module intentionally does not follow that older pattern).

defusedxml's rejection exceptions are normalised to ET.ParseError so
existing ``except ET.ParseError:`` call sites keep working unmodified.
"""
from __future__ import annotations

import logging
import os
from typing import IO, Union
from xml.etree import ElementTree as ET

log = logging.getLogger('feedBack.plugin.feedpakr')

# Mirrors the source type xml.etree.ElementTree.parse() itself accepts: a
# path (str/bytes/os.PathLike) or an already-open readable file object.
XMLSource = Union[str, bytes, 'os.PathLike[str]', 'os.PathLike[bytes]', IO[bytes], IO[str]]

try:
    import defusedxml.ElementTree as _safe_ET
    from defusedxml.common import DefusedXmlException as _DefusedXmlException
    _HAVE_DEFUSEDXML = True
except ImportError:
    _safe_ET = None
    _DefusedXmlException = ()
    _HAVE_DEFUSEDXML = False

# Log at most once, on first actual use rather than at import time — most
# processes that import this module do go on to parse untrusted XML with
# it, but logging unconditionally at import would also fire for e.g. a
# one-off script that imports feedpakr_lyrics/feedpakr_tones for an
# unrelated helper and never calls safe_parse at all. safe_parse() itself
# still raises on every call regardless of whether this has already fired.
_logged_missing_defusedxml = False


def _log_missing_defusedxml_once() -> None:
    global _logged_missing_defusedxml
    if _logged_missing_defusedxml:
        return
    _logged_missing_defusedxml = True
    log.error(
        'feedpakr_safe_xml: defusedxml is not installed — refusing to parse '
        'untrusted XML with it. Install defusedxml (a requirements.txt '
        'dependency of this plugin) to restore GP-import lyrics/tones '
        'extraction.'
    )


class MissingHardenedParserError(RuntimeError):
    """Raised by safe_parse() when defusedxml isn't installed. Deliberately
    NOT a subclass of ET.ParseError — this is a missing-dependency/ops
    problem, not "this particular XML failed to parse", so it must not be
    swallowed by an `except ET.ParseError:` written for the latter."""


def safe_parse(source: XMLSource, *args, **kwargs) -> ET.ElementTree:
    """Hardened equivalent of ``ET.parse(source, *args, **kwargs)``.

    Extra args/kwargs are forwarded as-is (e.g. ET.parse's ``parser=``, or
    defusedxml.ElementTree.parse's additional ``forbid_dtd=`` /
    ``forbid_entities=`` / ``forbid_external=`` knobs), so this stays a
    drop-in replacement as call sites evolve.

    Raises MissingHardenedParserError (fails closed) if defusedxml isn't
    installed, rather than silently parsing with unhardened stdlib ET.
    """
    if not _HAVE_DEFUSEDXML:
        _log_missing_defusedxml_once()
        raise MissingHardenedParserError(
            'defusedxml is not installed; refusing to parse untrusted XML '
            'without it (pip install defusedxml)'
        )
    try:
        return _safe_ET.parse(source, *args, **kwargs)
    except _DefusedXmlException as e:
        raise ET.ParseError(str(e)) from e
