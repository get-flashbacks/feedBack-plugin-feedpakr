"""feedpakr_safe_xml hardens the three untrusted-XML parse call sites
flagged by a security audit (feedpakr_lyrics.py:41,63, feedpakr_tones.py:58)
against entity-expansion ("billion laughs") DoS. xml.etree.ElementTree has
no built-in protection against this; defusedxml does, when installed (a
requirements.txt dependency now — see that file's comment on this).
"""

import xml.etree.ElementTree as ET

import pytest

import feedpakr_lyrics as lyrics_mod
import feedpakr_safe_xml as safe_xml
import feedpakr_tones as tones_mod

# The classic "billion laughs" payload. Kept small (lol4) so even an
# UNPROTECTED stdlib parse wouldn't hang the test suite — the assertion is
# about REJECTION, not about surviving a full-size attack.
BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<vocals>&lol4;</vocals>
"""

# The same DTD in a <song>-shaped payload with a real <tonebase>/<tone>:
# an UNHARDENED stdlib parse would expand <tone name="&lol4;"/> and return
# non-None data, so the parse_tones_xml rejection test genuinely exercises
# the guard (the plain <vocals> payload above yields None either way — no
# <tonebase>/<tones> means parse_tones_xml returns None on its own).
TONES_SHAPED_BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<song version="7">
  <tonebase>Clean Guitar</tonebase>
  <tones count="1">
    <tone id="0" name="&lol4;" time="0.0"/>
  </tones>
</song>
"""


def test_defusedxml_is_actually_installed():
    # If this ever regresses (e.g. a requirements sync drops it), every
    # other assertion here would silently start exercising the unhardened
    # stdlib fallback instead.
    assert safe_xml._HAVE_DEFUSEDXML, (
        "defusedxml not installed — feedpakr_safe_xml is silently running "
        "unhardened. Check requirements.txt."
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
    something the existing except clause doesn't catch. Uses the
    <tones>-shaped payload so an unhardened parse would yield real
    data (non-None) — otherwise this test could pass with the
    hardening reverted, just pinning "no hang" instead."""
    p = tmp_path / "bomb.xml"
    p.write_text(TONES_SHAPED_BOMB, encoding="utf-8")
    assert tones_mod.parse_tones_xml(str(p)) is None


def test_parse_vocals_xml_still_works_on_normal_xml(tmp_path):
    p = tmp_path / "vocals.xml"
    p.write_text(
        '<vocals><vocal time="1.5" length="0.25" lyric="la"/></vocals>',
        encoding="utf-8",
    )
    assert lyrics_mod.parse_vocals_xml(str(p)) == [{"t": 1.5, "d": 0.25, "w": "la"}]
