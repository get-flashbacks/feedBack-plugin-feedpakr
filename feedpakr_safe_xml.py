"""Hardened XML parsing shared by feedpakr_lyrics.py and feedpakr_tones.py
(security audit: XXE / entity-expansion in xml.etree.ElementTree.parse).

Both callers parse arrangement XML that gp2rs_gpx.convert_file wrote from
an uploaded, untrusted GP/GPX source — a crafted file could carry a DOCTYPE
with external entities or nested entity expansion ("billion laughs").
xml.etree.ElementTree resolves both; defusedxml.ElementTree hardens
parsing against them when installed, falling back to stdlib with a logged
warning otherwise (same pattern already used by the feedBack host's own
lib/gp8_audio_sync.py / lib/gp_autosync.py).

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

# Warn at most once, on first actual use rather than at import time — most
# processes that import this module do go on to parse untrusted XML with
# it, but warning unconditionally at import would also fire for e.g. a
# one-off script that imports feedpakr_lyrics/feedpakr_tones for an
# unrelated helper and never calls safe_parse at all.
_warned_missing_defusedxml = False


def _warn_missing_defusedxml_once() -> None:
    global _warned_missing_defusedxml
    if _warned_missing_defusedxml:
        return
    _warned_missing_defusedxml = True
    log.warning(
        'feedpakr_safe_xml: defusedxml not installed; parsing untrusted XML '
        'with stdlib xml.etree (install defusedxml for hardened parsing)'
    )


def safe_parse(source: XMLSource, *args, **kwargs) -> ET.ElementTree:
    """Hardened equivalent of ``ET.parse(source, *args, **kwargs)``.

    Extra args/kwargs are forwarded as-is (e.g. ET.parse's ``parser=``, or
    defusedxml.ElementTree.parse's additional ``forbid_dtd=`` /
    ``forbid_entities=`` / ``forbid_external=`` knobs), so this stays a
    drop-in replacement as call sites evolve.
    """
    if not _HAVE_DEFUSEDXML:
        _warn_missing_defusedxml_once()
        # No defusedxml available to fall back to — this is the
        # unavoidable degraded path (see the module docstring / the
        # warning above), not a spot that could use it instead.
        return ET.parse(source, *args, **kwargs)
    try:
        return _safe_ET.parse(source, *args, **kwargs)
    except _DefusedXmlException as e:
        raise ET.ParseError(str(e)) from e
