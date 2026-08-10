"""Tests for feedpakr_lyrics.reconstruct_plain_text() — pure function, no
host core lib needed, so unlike test_pipeline.py this always runs."""

import feedpakr_lyrics as lyrics_mod


def test_reconstruct_plain_text_joins_words_with_spaces():
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'hello'},
        {'t': 0.5, 'd': 0.5, 'w': 'world+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'hello world'


def test_reconstruct_plain_text_handles_multiple_lines():
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'first'},
        {'t': 0.5, 'd': 0.5, 'w': 'line+'},
        {'t': 1.0, 'd': 0.5, 'w': 'second'},
        {'t': 1.5, 'd': 0.5, 'w': 'line+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'first line\nsecond line'


def test_reconstruct_plain_text_joins_syllables_without_space_on_trailing_dash():
    # "beau-" + "tiful+" -> "beautiful" (mid-word split, no space)
    entries = [
        {'t': 0.0, 'd': 0.2, 'w': 'beau-'},
        {'t': 0.2, 'd': 0.3, 'w': 'tiful+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'beautiful'


def test_reconstruct_plain_text_flushes_trailing_line_without_plus_marker():
    # Last line has no trailing "+" entry — must not be dropped.
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'no'},
        {'t': 0.5, 'd': 0.5, 'w': 'trailing'},
        {'t': 1.0, 'd': 0.5, 'w': 'marker'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'no trailing marker'


def test_reconstruct_plain_text_skips_empty_words():
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'a'},
        {'t': 0.5, 'd': 0.5, 'w': ''},
        {'t': 1.0, 'd': 0.5, 'w': 'b+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'a b'


def test_reconstruct_plain_text_empty_input():
    assert lyrics_mod.reconstruct_plain_text([]) == ''


def test_reconstruct_plain_text_skips_none_entries():
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'a'},
        None,
        {'t': 1.0, 'd': 0.5, 'w': 'b+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'a b'


def test_reconstruct_plain_text_skips_entries_missing_w():
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'a'},
        {'t': 0.5, 'd': 0.5},
        {'t': 1.0, 'd': 0.5, 'w': 'b+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'a b'


def test_reconstruct_plain_text_coerces_non_string_w():
    entries = [
        {'t': 0.0, 'd': 0.5, 'w': 'count'},
        {'t': 0.5, 'd': 0.5, 'w': 5},
        {'t': 1.0, 'd': 0.5, 'w': 'end+'},
    ]
    assert lyrics_mod.reconstruct_plain_text(entries) == 'count 5 end'
