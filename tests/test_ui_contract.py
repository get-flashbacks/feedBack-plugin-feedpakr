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

    assert "_handoffAvailability = { preview: null, split: null, difficulty: null }" in script
    assert "/api/plugins/difficulty_ladder/generate" in script
    assert "Generate Difficulty Ladder" in script
    assert "JSON.stringify({ filename: relPath, arrangement_index: 0, force: false })" in script
    assert "!f.phrase_ladder" in script
