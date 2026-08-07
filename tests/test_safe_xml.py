"""feedpakr_safe_xml hardens the three untrusted-XML parse call sites
flagged by a security audit (feedpakr_lyrics.py:41,63, feedpakr_tones.py:58)
against entity-expansion ("billion laughs") DoS. xml.etree.ElementTree has
no built-in protection against this; defusedxml does (a requirements.txt
dependency), and safe_parse fails closed if it's somehow absent rather than
reverting to the vulnerable stdlib parser.
"""

import xml.etree.ElementTree as ET

import pytest

import feedpakr_lyrics as lyrics_mod
import feedpakr_safe_xml as safe_xml
import feedpakr_tones as tones_mod

# The classic "billion laughs" DTD, shared by the payloads below. Kept small
# (lol4) so even an UNPROTECTED stdlib parse wouldn't hang the test suite —
# the assertion is about REJECTION, not about surviving a full-size attack.
_ENTITY_DTD = """<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
"""

BILLION_LAUGHS = '<?xml version="1.0"?>\n' + _ENTITY_DTD + '<vocals>&lol4;</vocals>\n'

# A tones-shaped variant with the expansion inside <tonebase> — an
# UNPROTECTED stdlib parse of this would SUCCEED and return a non-None
# result, so the tones call-site test can only pass when the hardening
# actually rejects the payload (the <vocals> variant can't make that
# distinction: parse_tones_xml() returns None for it either way).
TONES_BILLION_LAUGHS = (
    '<?xml version="1.0"?>\n' + _ENTITY_DTD + '<song><tonebase>&lol4;</tonebase></song>\n'
)


def test_defusedxml_is_actually_installed():
    # If this ever regresses (e.g. a requirements sync drops it), every
    # other rejection test here would fail closed (safe_parse raises) — this
    # guard gives a clearer error than the resulting cascade.
    assert safe_xml._HAVE_DEFUSEDXML, (
        "defusedxml not installed — feedpakr_safe_xml refuses to parse "
        "untrusted XML. Check requirements.txt."
    )


def test_safe_parse_rejects_billion_laughs(tmp_path):
    p = tmp_path / "bomb.xml"
    p.write_text(BILLION_LAUGHS, encoding="utf-8")
    with pytest.raises(ET.ParseError):
        safe_xml.safe_parse(str(p))


def test_safe_parse_still_parses_normal_xml(tmp_path):
    p = tmp_path / "vocals.xml"
    p.write_text('<vocals><vocal time="0" length="0.5" lyric="hi" note="60"/></vocals>', encoding="utf-8")
    root = safe_xml.safe_parse(str(p)).getroot()
    assert root.tag == "vocals"


def test_safe_parse_fails_closed_without_defusedxml(monkeypatch, tmp_path):
    """feedpakr_safe_xml.py:38 — if defusedxml is somehow absent at runtime
    safe_parse must NOT silently fall back to the vulnerable stdlib parser;
    it fails closed so untrusted XML is never expanded unhardened."""
    monkeypatch.setattr(safe_xml, "_HAVE_DEFUSEDXML", False)
    p = tmp_path / "vocals.xml"
    p.write_text('<vocals><vocal lyric="hi"/></vocals>', encoding="utf-8")
    with pytest.raises(ET.ParseError):
        safe_xml.safe_parse(str(p))


def test_parse_vocals_xml_rejects_billion_laughs(tmp_path):
    """feedpakr_lyrics.py:41 — a malicious gp2rs_gpx-shaped vocals XML must
    not silently expand; it must raise instead of hanging/exhausting memory."""
    p = tmp_path / "bomb.xml"
    p.write_text(BILLION_LAUGHS, encoding="utf-8")
    with pytest.raises(ET.ParseError):
        lyrics_mod.parse_vocals_xml(str(p))


def test_parse_vocal_pitch_xml_rejects_billion_laughs(tmp_path):
    """feedpakr_lyrics.py:63."""
    p = tmp_path / "bomb.xml"
    p.write_text(BILLION_LAUGHS, encoding="utf-8")
    with pytest.raises(ET.ParseError):
        lyrics_mod.parse_vocal_pitch_xml(str(p))


def test_parse_tones_xml_treats_billion_laughs_as_unparseable(tmp_path):
    """feedpakr_tones.py:58 — parse_tones_xml() already catches
    ET.ParseError and returns None for malformed XML; a rejected
    entity-expansion attack must degrade the same way, not raise
    something the existing except clause doesn't catch. Tones-shaped
    payload (the expansion inside <tonebase>) so an unhardened parse
    would succeed and return data — only the hardened rejection can
    produce None here."""
    p = tmp_path / "bomb.xml"
    p.write_text(TONES_BILLION_LAUGHS, encoding="utf-8")
    assert tones_mod.parse_tones_xml(str(p)) is None


def test_parse_vocals_xml_still_works_on_normal_xml(tmp_path):
    p = tmp_path / "vocals.xml"
    p.write_text(
        '<vocals><vocal time="1.5" length="0.25" lyric="la"/></vocals>',
        encoding="utf-8",
    )
    assert lyrics_mod.parse_vocals_xml(str(p)) == [{"t": 1.5, "d": 0.25, "w": "la"}]
