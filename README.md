# feedpakr

A [feedBack](https://github.com/got-feedBack/feedBack) plugin that imports Guitar Pro
files into `.feedpak` at maximum fidelity, and (in a later phase) upgrades existing
`.sloppak` files to `.feedpak`.

Plugin id: `feedpakr`. Install into `plugins/feedpakr/` (folder name must match the id).

## Status: Phase 2 (GP3-GP8, all audio modes, lyrics)

- Upload a `.gp3`-`.gp5` (pyguitarpro) or `.gp6`/`.gpx`/`.gp` (GPIF, GP6-GP8) file, pick
  which tracks to include and name their arrangements, attach cover art.
- Audio: synthesize with FluidSynth (.gp3-.gp5 only), use a GP8 file's embedded backing
  track, or sync a user-uploaded/YouTube-fetched recording to the chart via chroma-DTW
  autosync — or skip audio entirely.
- Produces a `.feedpak` with real notes/chords/anchors/handshapes/templates per
  arrangement, a corrected per-track **capo** for both source families (GP3-5's
  `Track.offset` and GPIF's `CapoFret` property — the legacy GP→sloppak pipeline
  hardcoded this to 0 for everything), a `song_timeline.json` with accurate **sections
  and beats**, and **lyrics** — precisely-timed from a GPIF vocal track's own `<vocals>`
  XML, or approximated from GP3-5's single per-measure `song.lyrics` blob (labeled as
  such; GP3-5 has no per-syllable timing to work with).
- Every enrichment step is best-effort: a failure degrades that one feature (recorded
  as a warning) rather than aborting the import. Output is self-validated against the
  vendored feedpak-spec JSON Schemas (`assets/schemas/`) before being written.
- A pack built without audio is a valid *authoring intermediate* (feedpak spec §5.3.2)
  but will not pass strict validation (`stems` is schema-required) until audio is added.
- **Known limitation, surfaced as a warning, not hidden**: GPIF repeat/volta expansion
  isn't implemented in the host core yet (`gp2rs_gpx.convert_file`'s own docstring says
  so) — a GP6/7/8 file that uses repeats gets a single unexpanded pass, so its
  `song_timeline.json` timing won't match an equivalent .gp5 import of the same song.
  feedpakr detects this up front and tells the user, rather than silently producing
  drifted timing.

**Not yet implemented** (see the plan for the full phased roadmap):
`keys.json` / tones / notation sidecars / `vocal_pitch.json` / drums-as-arrangements,
the `.sloppak` → `.feedpak` batch upgrade tab, and handoffs to the `song-preview` /
`stem-splitter` plugins.

## Why

feedBack's existing GP import pipeline (documented in a data-loss audit predating this
plugin) silently drops sections, beats, tones, capo, key/time signatures, and more.
The `.feedpak` format (spec ≥1.19.0) can hold all of it; feedpakr's job is to actually
put it there, without needing any changes to the feedBack core.

## Architecture

- `routes.py` — thin FastAPI routes (upload / upload-cover / upload-audio /
  youtube-audio / autosync-preview / `WS build`), following the upload-token /
  streaming-build pattern used by `feedBack-plugin-musicxml-import`.
- `feedpakr_pipeline.py` — orchestrates parsing, conversion (via the host's `gp2rs` /
  `gp2rs_gpx` / `song`), and fidelity enrichment (capo for both source families,
  `song_timeline.json`, repeat-markup detection, vocals/lyrics routing).
- `feedpakr_audio.py` — MIDI synthesis, GP8 embedded audio extraction, autosync,
  YouTube fetch, and OGG normalization. Every function degrades to `(None, ..., error)`
  rather than raising.
- `feedpakr_lyrics.py` — lyrics extraction for both source families (see above).
- `feedpakr_pack.py` — manifest assembly and `.feedpak` zip writing. Pure dicts, no
  pyguitarpro dependency — easy to unit test.
- `feedpakr_validate.py` — validates manifest/arrangement/song_timeline payloads against
  the vendored feedpak-spec schemas, always returning a report rather than raising.

`routes.py` loads its siblings via `context['load_sibling']` (not bare imports) to avoid
the plugin-module-name collisions that mechanism exists to prevent.

### A note on the GPIF (.gp6/.gpx/.gp) path

`gp2rs.list_tracks` / `gp2rs.convert_file` already dispatch to `gp2rs_gpx` internally
based on file extension, so most of the pipeline is written against `gp2rs`'s uniform
surface. Three things needed format-specific handling because pyguitarpro has no model
for them on the GPIF side at all:

- **Capo** lives at `Track/Staves/Staff/Properties/Property[@name='CapoFret']/Fret` in
  the raw GPIF XML — nothing in the host reads it, so feedpakr does, via
  `gp2rs_gpx._load_gpif` + `_gpif_tracks` (private but stable — the module's only way to
  reach a raw `<Track>` element).
- **Vocal tracks** produce a `<vocals>` XML (via `gp2rs_gpx.convert_vocal_track`
  internally), not the `<song>` shape `song.parse_arrangement` expects. feedpakr peeks at
  each output XML's root tag and routes `<vocals>` files to `feedpakr_lyrics.py` instead
  — routing them through `song.parse_arrangement` by mistake was a real bug caught while
  building this (an empty-named ghost arrangement, no lyrics produced at all; now a
  regression test in `tests/test_pipeline.py`).
- **Piano LH/RH pairing** (`gp2rs_gpx._find_piano_pairs`) can silently drop a track index
  from `convert_file`'s output when it's merged into its RH partner — feedpakr
  independently computes the same filtered order up front (`_output_track_order`) so the
  capo/lyrics-routing loop can still zip() output XMLs to the right source track.

## Tests

```bash
pip install jsonschema
pytest
```

`tests/test_pipeline.py` needs the feedBack host's `lib/` (for `guitarpro`, `gp2rs`,
`gp2rs_gpx`, `song`) on `sys.path` — `tests/conftest.py` looks for a sibling
`pakr/feedBack` checkout and self-skips those tests if it isn't found. Notable
regression tests, both against real sample files:

- `test_build_feedpak_extracts_all_16_sections` — "Money (J).gp5" was documented as
  losing all 16 of its section markers through the legacy sloppak pipeline; must extract
  all 16.
- `test_build_feedpak_gpif_vocal_track_becomes_lyrics_not_arrangement` — locks in the
  `<vocals>`-routing fix described above, against the GP8 companion file.
- `test_gpif_capo_lookup_reads_capo_fret` — capo extraction against a real capo'd fixture,
  not just an empty-dict check.

## License

MIT
