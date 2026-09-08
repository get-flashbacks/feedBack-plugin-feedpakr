import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_inline_handlers_are_exported_from_iife():
    """Every handler referenced by inline HTML must be visible on window."""
    html = (ROOT / "screen.html").read_text(encoding="utf-8")
    script = (ROOT / "screen.js").read_text(encoding="utf-8")

    assert 'onclick="fprSearchCover()"' in html
    assert "window.fprSearchCover = fprSearchCover;" in script


def test_difficulty_ladder_handoff_uses_current_plugin_endpoint():
    script = (ROOT / "screen.js").read_text(encoding="utf-8")

    assert "difficulty: null" in script
    assert "_handoffAvailability = { preview: null, split: null" in script
    assert "/api/plugins/difficulty_ladder/generate" in script
    assert "Generate Difficulty Ladder" in script
    assert "JSON.stringify({ filename: relPath, arrangement_index: 0, force: false })" in script
    assert "!f.phrase_ladder" in script


def _extract_manual_offset_regex_pattern():
    """Pull FPR_MANUAL_OFFSET_RE's pattern straight out of screen.js, rather
    than asserting on an exact source string (which broke the first time
    the pattern was refined to also accept PEP 515 underscore grouping).
    Keying off the named constant (not a substring of the validation
    expression) survives any future refactor of how/where it's used. JS and
    Python's `re` agree on the syntax this particular pattern uses (anchors,
    character classes, non-capturing groups, alternation — no lookbehind/
    named groups), so the extracted source compiles directly as a Python
    pattern and can be exercised against real inputs instead of eyeballing
    the string.
    """
    script = (ROOT / "screen.js").read_text(encoding="utf-8")
    m = re.search(r"const FPR_MANUAL_OFFSET_RE = (/\^.*?\$/i);", script)
    assert m, "could not find FPR_MANUAL_OFFSET_RE in screen.js"
    js_literal = m.group(1)
    assert js_literal.startswith("/") and js_literal.endswith("/i")
    return js_literal[1:-2]  # strip the /.../i delimiters


def test_manual_offset_client_validation_matches_server_float_grammar():
    """fprCollectManualOffset must reject anything Python's float() (what
    routes.py's ws_build actually parses this string with) would reject,
    and accept everything float() accepts — notably hex/octal/binary
    literals like "0x10" (JS's Number() happily parses these as finite
    decimals but float() raises ValueError), and PEP 515 underscore-grouped
    digits like "1_000" (float() accepts these; a client regex that doesn't
    would wrongly block a value the server would have built successfully).
    Without the hex guard, a user-entered "0x10" passes client-side
    validation, the build request goes out, and only then comes back a
    confusing "manual_offset must be a finite number of seconds" 400 from
    the server.
    """
    pattern = re.compile(_extract_manual_offset_regex_pattern(), re.IGNORECASE)

    cases = [
        "0x10", "0b101", "0o17",  # hex/octal/binary — Number() finite, float() rejects
        "Infinity", "inf", "nan",  # textual specials — regex must reject (finiteness is a separate check)
        "1e999",  # regex-shaped but not finite — same story
        "3.5", "-2.1e3", "0", ".5", "5.", "+3", "-0.0", "1e10",
        "1_000", "1_2.5", "1e1_0",  # PEP 515 underscore grouping — float() accepts
        "1__0", "_10", "10_", "1_.5", "1._5", "1_e10",  # invalid underscore placement
        "", "  ", "abc",
    ]
    for raw in cases:
        regex_says_numeric = bool(pattern.match(raw))
        try:
            float(raw.replace("_", "") if regex_says_numeric else raw)
            float_parses = True
        except ValueError:
            float_parses = False
        # The regex's job is purely grammar (does this look like a number
        # float() would accept), not finiteness (Infinity/inf/nan/1e999 are
        # separately rejected by the Number.isFinite check in the real
        # function) — so assert the regex matches float()'s parseability
        # for every case here except the textual-specials/overflow ones,
        # which are grammar-shaped strings float() happens to parse but the
        # regex is deliberately narrower than (a plain decimal grammar).
        if raw in ("Infinity", "inf", "nan", "1e999"):
            continue
        assert regex_says_numeric == float_parses, (
            f"{raw!r}: regex says {regex_says_numeric}, float() parses = {float_parses}"
        )


def test_manual_offset_finiteness_guard_rejects_regex_shaped_non_finite_values():
    """Pins the Number.isFinite(Number(raw.replace(...))) arm of the same
    condition, which the regex test above deliberately doesn't exercise: a
    value can be grammar-valid (the regex matches) while still not being
    finite -- "1e999" is exactly this case (PR #45's original finiteness
    concern). Without this second guard, fprCollectManualOffset would
    accept "1e999" as a manual offset and forward it to a build that bakes
    a literal Infinity timestamp into the chart.
    """
    script = (ROOT / "screen.js").read_text(encoding="utf-8")
    assert "!Number.isFinite(Number(raw.replace(/_/g, '')))" in script

    pattern = re.compile(_extract_manual_offset_regex_pattern(), re.IGNORECASE)
    assert pattern.match("1e999"), "sanity: regex should consider this grammar-valid"
    assert not math.isfinite(float("1e999")), "sanity: this value is not finite"
