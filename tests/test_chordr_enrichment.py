import pytest

import feedpakr_pipeline as pipeline


def test_chordr_fills_only_unnamed_templates_and_uses_chart_context():
    wire = {
        "chords": [
            {"id": 0, "notes": [{"s": 0, "f": 0}, {"s": 1, "f": 2}]},
            {"id": 1, "notes": [{"s": 0, "f": 3}, {"s": 1, "f": 2}]},
        ],
        "templates": [
            {"frets": [0, 2, -1], "name": ""},
            {"frets": [3, 2, -1], "name": "Authored"},
        ],
    }
    calls = []

    def analyze(chords, *, context, templates):
        calls.append((chords, context, templates))
        return {"resolvedNames": ["E5", "G5"]}

    count = pipeline._enhance_chord_template_names(
        wire, analyze, tuning=[0, 0, 0], capo=2, is_bass=True,
    )

    assert count == 1
    assert wire["templates"][0]["name"] == "E5"
    assert wire["templates"][0]["displayName"] == "E5"
    assert wire["templates"][1]["name"] == "Authored"
    assert calls[0][1] == {
        "tuning": [0, 0, 0], "capo": 2, "stringCount": 3, "isBass": True,
    }


def test_chordr_skips_a_template_when_its_events_disagree():
    wire = {
        "chords": [{"id": 0}, {"id": 0}],
        "templates": [{"name": ""}],
    }
    count = pipeline._enhance_chord_template_names(
        wire, lambda *args, **kwargs: {"resolvedNames": ["C", "G"]},
        tuning=[], capo=0, is_bass=False,
    )
    assert count == 0
    assert wire["templates"][0]["name"] == ""


def test_chordr_rejects_malformed_analysis_result():
    with pytest.raises(ValueError, match="invalid name list"):
        pipeline._enhance_chord_template_names(
            {"chords": [{"id": 0}], "templates": [{"name": ""}]},
            lambda *args, **kwargs: {"resolvedNames": []},
            tuning=[], capo=0, is_bass=False,
        )
