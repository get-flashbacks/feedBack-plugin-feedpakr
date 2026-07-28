# feedpakr

A [feedBack](https://github.com/got-feedBack/feedBack) plugin that imports Guitar Pro
files into `.feedpak` at maximum fidelity, and (in a later phase) upgrades existing
`.sloppak` files to `.feedpak`.

Plugin id: `feedpakr`. Install into `plugins/feedpakr/` (folder name must match the id).

## Status: Phase 3 (max fidelity)

- Upload a `.gp3`-`.gp5` (pyguitarpro) or `.gp6`/`.gpx`/`.gp` (GPIF, GP6-GP8) file, pick
  which tracks to include and name their arrangements, attach cover art.
- Audio: synthesize with FluidSynth (.gp3-.gp5 only), use a GP8 file's embedded backing
  track, or sync a user-uploaded/YouTube-fetched recording to the chart via chroma-DTW
  autosync — or skip audio entirely.
- Every enrichment step is best-effort: a failure degrades that one feature (recorded
  as a warning) rather than aborting the import. Output is self-validated against the
  vendored feedpak-spec JSON Schemas (`assets/schemas/`) before being written. The build
  result reports which features actually landed (`features: {...}`), shown in the UI as
  a "Captured: …" summary rather than promising more than the source actually had.
- A pack built without audio is a valid *authoring intermediate* (feedpak spec §5.3.2)
  but will not pass strict validation (`stems` is schema-required) until audio is added.

### What gets captured, and from where

| Fidelity gap (from the original data-loss audit) | Status | Source |
|---|---|---|
| Capo | **Fixed**, both source families | GP3-5 `Track.offset`; GPIF `CapoFret` property — gp2rs/gp2rs_gpx hardcode/ignore this |
| Sections, beats | **Fixed** | `song_timeline.json`, via `song.load_song()` on the converted (playback-schedule-accurate) XML |
| Hand shapes | **Derived**, both families | gp2rs never populates these ("empty for now") — feedpakr groups consecutive same-chord hits into contiguous spans |
| Tone changes | **Fixed** where the source has them | GPIF: read back from XML gp2rs_gpx already injects but `song.parse_arrangement` silently drops; GP3-5: scanned from every selected track's `mixTableChange`, not just the first (unlike the legacy importer this project replaces) |
| Key signature | **New capability** | `keys.json`, GP3-5 `MeasureHeader.keySignature` / GPIF `MasterBar/Key`, spelled via the standard circle-of-fifths table |
| Lyrics | **Fixed** (GPIF), **approximated** (GP3-5, labeled as such) | GPIF vocal track's own `<vocals>` XML (exact timing); GP3-5's single per-measure `song.lyrics` blob has no per-syllable timing to work with |
| Vocal pitch | **New capability** (GPIF only) | Read directly from the same `<vocals>` XML's `note=` attribute |
| Drums | **Promoted to real drum arrangements** (GP3-5 only) | `gp2rs.convert_drum_track_to_drumtab` → `type: drums` + `drum_tab.json`, instead of the legacy string\*24+fret fretted encoding. GPIF drums have no host-side equivalent yet, so they stay fret-encoded |
| Piano notation | **New capability** (GPIF keys tracks) | `notation_<id>.json` via `gp2notation.convert_track_to_notation`; single-hand only, no LH/RH two-stave merge yet |
| Repeats/D.S./D.C./Coda | **Fixed** (GP3-5), **known limitation** (GPIF) | gp2rs already expands these; gp2rs_gpx doesn't yet (host TODO) — feedpakr detects repeat/volta markup up front and warns rather than silently shipping drifted timing |
| Rigs/gear chains, harmony | Out of scope | GP carries no rig/signal-chain data; harmony (chord *intent* vs. what's played) needs separate work |

**Not yet implemented**: the `.sloppak` → `.feedpak` batch upgrade tab, and handoffs to
the `song-preview` / `stem-splitter` plugins (Phase 4).

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
  `gp2rs_gpx` / `song`), and all fidelity enrichment (capo, timeline, lyrics, keys,
  tones, handshapes, drums-as-arrangements, notation, vocal pitch).
- `feedpakr_audio.py` — MIDI synthesis, GP8 embedded audio extraction, autosync,
  YouTube fetch, and OGG normalization. Every function degrades to `(None, ..., error)`
  rather than raising.
- `feedpakr_lyrics.py` — lyrics + vocal pitch, both read from the same GPIF `<vocals>`
  XML where available; GP3-5 approximation otherwise.
- `feedpakr_tones.py` — tone-change extraction for both source families.
- `feedpakr_keys.py` — key/scale extraction (`keys.json`) for both source families.
- `feedpakr_handshapes.py` — hand-shape derivation from chord data.
- `feedpakr_notation.py` — GPIF keys/piano track notation sidecars.
- `feedpakr_pack.py` — manifest assembly and `.feedpak` zip writing. Pure dicts, no
  pyguitarpro dependency — easy to unit test.
- `feedpakr_validate.py` — validates every payload shape (manifest, arrangement,
  song_timeline, keys, vocal_pitch, drum_tab, notation) against the vendored feedpak-spec
  schemas, always returning a report rather than raising.

`routes.py` loads its siblings via `context['load_sibling']` (not bare imports) to avoid
the plugin-module-name collisions that mechanism exists to prevent.

### A note on the GPIF (.gp6/.gpx/.gp) path

`gp2rs.list_tracks` / `gp2rs.convert_file` already dispatch to `gp2rs_gpx` internally
based on file extension, so most of the pipeline is written against `gp2rs`'s uniform
surface. A few things needed format-specific handling because pyguitarpro has no model
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
  regression test in `tests/test_pipeline.py`). The same `<vocals>` XML also feeds
  `vocal_pitch.json` directly (its `note=` attribute is already the sung MIDI pitch) —
  no need for gp2rs_gpx's separate, much more involved private pitch-sidecar function.
- **Piano LH/RH pairing** (`gp2rs_gpx._find_piano_pairs`) can silently drop a track index
  from `convert_file`'s output when it's merged into its RH partner — feedpakr
  independently computes the same filtered order up front (`_output_track_order`) so the
  capo/lyrics/notation routing can still zip() output XMLs to the right source track.
- **`<tones>` is written but never read**: `gp2rs_gpx.convert_file` already injects a
  `<tonebase>`/`<tones>` block into GPIF-converted XML (via its own internal
  `_collect_tone_events`/`_inject_tones`) — but `song.parse_arrangement` has no code path
  that reads those elements back out, so the data was being silently discarded on the way
  to the wire format. `feedpakr_tones.parse_tones_xml` closes that gap.

## Tests

```bash
pip install jsonschema
pytest
```

`tests/test_pipeline.py` needs the feedBack host's `lib/` (for `guitarpro`, `gp2rs`,
`gp2rs_gpx`, `song`) on `sys.path` — `tests/conftest.py` looks for a sibling
`pakr/feedBack` checkout and self-skips those tests if it isn't found. Notable
regression tests, all against real sample files:

- `test_build_feedpak_extracts_all_16_sections` — "Money (J).gp5" was documented as
  losing all 16 of its section markers through the legacy sloppak pipeline; must extract
  all 16.
- `test_build_feedpak_gpif_vocal_track_becomes_lyrics_not_arrangement` — locks in the
  `<vocals>`-routing fix described above, against the GP8 companion file.
- `test_gpif_capo_lookup_reads_capo_fret` — capo extraction against a real capo'd fixture,
  not just an empty-dict check.
- `test_build_feedpak_gp345_drums_become_type_drums_arrangement`,
  `test_build_feedpak_handshapes_derived_from_chords`,
  `test_build_feedpak_gpif_keys_track_gets_notation`,
  `test_build_feedpak_gpif_vocal_track_produces_vocal_pitch` — Phase 3 fidelity features,
  each checked against real converted output, not just unit-level logic.

## License

MIT
