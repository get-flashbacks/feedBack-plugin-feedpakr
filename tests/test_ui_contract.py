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


def test_manual_offset_client_validation_matches_server_float_grammar():
    """fprCollectManualOffset must reject anything Python's float() (what
    routes.py's ws_build actually parses this string with) would reject —
    notably hex/octal/binary literals like "0x10", which JS's Number()
    happily parses as a finite decimal but float() raises ValueError on.
    Without this, a user-entered "0x10" passes client-side validation, the
    build request goes out, and only then comes back a confusing
    "manual_offset must be a finite number of seconds" 400 from the server.
    """
    script = (ROOT / "screen.js").read_text(encoding="utf-8")
    assert r"/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(raw)" in script
