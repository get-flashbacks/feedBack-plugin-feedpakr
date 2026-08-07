"""Hardened XML parsing shared by feedpakr_lyrics.py and feedpakr_tones.py
(security audit: XXE / entity-expansion in xml.etree.ElementTree.parse).

Both callers parse arrangement XML that gp2rs_gpx.convert_file wrote from
an uploaded, untrusted GP/GPX source — a crafted file could carry a DOCTYPE
with external entities or nested entity expansion ("billion laughs").
xml.etree.ElementTree resolves both; defusedxml.ElementTree hardens
parsing against them. defusedxml is a hard requirement (declared in
requirements.txt): if it's somehow absent at runtime this fails closed —
raising ET.ParseError rather than reverting to the vulnerable stdlib
parser — and a warning is logged at import time.

defusedxml's rejection exceptions are normalised to ET.ParseError so
existing ``except ET.ParseError:`` call sites keep working unmodified.
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

log = logging.getLogger('feedBack.plugin.feedpakr')

try:
    import defusedxml.ElementTree as _safe_ET
    from defusedxml.common import DefusedXmlException as _DefusedXmlException
    _HAVE_DEFUSEDXML = True
except ImportError:
    _safe_ET = None
    _DefusedXmlException = ()
    _HAVE_DEFUSEDXML = False
    log.warning(
        'feedpakr_safe_xml: defusedxml not installed; parsing untrusted XML '
        'with stdlib xml.etree (install defusedxml for hardened parsing)'
    )


def safe_parse(source: str) -> ET.ElementTree:
    """Hardened equivalent of ``ET.parse(source)`` — fails closed if
    defusedxml is missing rather than parsing untrusted XML unhardened."""
    if not _HAVE_DEFUSEDXML:
        raise ET.ParseError(
            'defusedxml not installed; refusing to parse untrusted XML with '
            'stdlib xml.etree (install defusedxml for hardened parsing)'
        )
    try:
        return _safe_ET.parse(source)
    except _DefusedXmlException as e:
        raise ET.ParseError(str(e)) from e
