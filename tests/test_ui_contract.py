from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_inline_handlers_are_exported_from_iife():
    """Every handler referenced by inline HTML must be visible on window."""
    html = (ROOT / "screen.html").read_text(encoding="utf-8")
    script = (ROOT / "screen.js").read_text(encoding="utf-8")

    assert 'onclick="fprSearchCover()"' in html
    assert "window.fprSearchCover = fprSearchCover;" in script
