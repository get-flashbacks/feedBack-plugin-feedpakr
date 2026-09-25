from types import SimpleNamespace

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


def test_build_feedpak_only_enhances_fretted_tracks(monkeypatch, tmp_path):
    xml_paths = []
    for index in range(2):
        xml_path = tmp_path / f"track-{index}.xml"
        xml_path.write_text("<?xml version='1.0'?><song/>", encoding="utf-8")
        xml_paths.append(str(xml_path))

    tracks = [
        {"index": 0, "name": "Piano", "is_piano": True, "is_drums": False},
        {"index": 1, "name": "Guitar", "is_piano": False, "is_drums": False},
    ]
    song = SimpleNamespace(
        tracks=[SimpleNamespace(offset=0), SimpleNamespace(offset=0)],
        lyrics=None,
    )
    arrangements = {
        xml_paths[0]: SimpleNamespace(name="Keys", capo=0, tuning=[0, 0, 0]),
        xml_paths[1]: SimpleNamespace(name="Guitar", capo=0, tuning=[0, 0, 0]),
    }
    wires = {
        "Keys": {
            "chords": [{
                "id": 0,
                "t": 0.0,
                "notes": [{"s": 0, "f": 0}, {"s": 1, "f": 2}],
            }],
            "templates": [{"marker": "piano", "name": ""}],
        },
        "Guitar": {
            "chords": [{
                "id": 0,
                "t": 0.0,
                "notes": [{"s": 0, "f": 0}, {"s": 1, "f": 2}],
            }],
            "templates": [{"marker": "guitar", "name": ""}],
        },
    }

    monkeypatch.setattr(pipeline, "_check_extension", lambda _path: None)
    monkeypatch.setattr(
        pipeline,
        "parse_gp",
        lambda _path: {
            "title": "Test",
            "artist": "",
            "album": "",
            "tempo": 120,
            "tracks": tracks,
            "format": "gp345",
            "has_embedded_audio": False,
        },
    )
    monkeypatch.setattr(pipeline, "guitarpro", SimpleNamespace(parse=lambda _path: song))
    monkeypatch.setattr(
        pipeline,
        "gp2rs",
        SimpleNamespace(convert_file=lambda *_args, **_kwargs: xml_paths),
    )
    monkeypatch.setattr(
        pipeline,
        "song_mod",
        SimpleNamespace(
            parse_arrangement=lambda path: arrangements[path],
            arrangement_to_wire=lambda arr: wires[arr.name],
            load_song=lambda _path: SimpleNamespace(beats=[], sections=[], song_length=1.0),
        ),
    )
    monkeypatch.setattr(pipeline.lyrics_mod, "is_vocals_xml", lambda _path: False)
    monkeypatch.setattr(pipeline.lyrics_mod, "extract_gp345_lyrics", lambda *_args: None)
    monkeypatch.setattr(pipeline.tones_mod, "extract_gp345_tones", lambda *_args: None)
    monkeypatch.setattr(pipeline.handshapes_mod, "derive_handshapes", lambda _wire: [])
    monkeypatch.setattr(pipeline.validate, "validate_pack", lambda **_kwargs: {})

    calls = []

    def analyze(chords, *, context, templates):
        calls.append(templates[0]["marker"])
        return {"resolvedNames": ["C"] * len(chords)}

    result = pipeline.build_feedpak(
        "test.gp5",
        track_indices=[0, 1],
        arrangement_names={0: "Keys", 1: "Guitar"},
        audio_mode="none",
        chordr_analyzer=analyze,
        enhance_chords=True,
    )

    assert calls == ["guitar"]
    assert result["features"]["chordr_names"] == 1
