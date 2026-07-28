# feedpakr

A [feedBack](https://github.com/got-feedBack/feedBack) plugin that imports Guitar Pro
files into `.feedpak` at maximum fidelity, and (in a later phase) upgrades existing
`.sloppak` files to `.feedpak`.

Plugin id: `feedpakr`. Install into `plugins/feedpakr/` (folder name must match the id).

## Status: Phase 1 (GP3/GP4/GP5 core path)

- Upload a `.gp3`/`.gp4`/`.gp5` file, pick which tracks to include and name their
  arrangements, optionally generate synthesized audio (FluidSynth) and attach cover art.
- Produces a `.feedpak` with real notes/chords/anchors/handshapes/templates per
  arrangement, a corrected per-track **capo** (the legacy GP→sloppak pipeline hardcoded
  this to 0), and a `song_timeline.json` with accurate, repeat-expansion-aware
  **sections and beats** — the flagship gap this project exists to close.
- Every enrichment step is best-effort: a failure degrades that one feature (recorded
  as a warning) rather than aborting the import. Output is self-validated against the
  vendored feedpak-spec JSON Schemas (`assets/schemas/`) before being written.
- A pack built without audio is a valid *authoring intermediate* (feedpak spec §5.3.2)
  but will not pass strict validation (`stems` is schema-required) until audio is added.

**Not yet implemented** (see the plan for the full phased roadmap):
GP6/GP7/GP8 (`.gpx`/`.gp`, GPIF path), audio autosync / GP8 embedded audio / YouTube
fetch, `keys.json` / tones / notation sidecars / `vocal_pitch.json` / drums-as-arrangements,
lyrics extraction, the `.sloppak` → `.feedpak` batch upgrade tab, and handoffs to the
`song-preview` / `stem-splitter` plugins.

## Why

feedBack's existing GP import pipeline (documented in a data-loss audit predating this
plugin) silently drops sections, beats, tones, capo, key/time signatures, and more.
The `.feedpak` format (spec ≥1.19.0) can hold all of it; feedpakr's job is to actually
put it there, without needing any changes to the feedBack core.

## Architecture

- `routes.py` — thin FastAPI routes (`/api/plugins/feedpakr/upload`,
  `/api/plugins/feedpakr/upload-cover`, `WS /ws/plugins/feedpakr/build`), following the
  upload-token / streaming-build pattern used by `feedBack-plugin-musicxml-import`.
- `feedpakr_pipeline.py` — orchestrates parsing, conversion (via the host's `gp2rs` /
  `song` / `gp2midi`), and fidelity enrichment (capo, `song_timeline.json`).
- `feedpakr_pack.py` — manifest assembly and `.feedpak` zip writing. Pure dicts, no
  pyguitarpro dependency — easy to unit test.
- `feedpakr_validate.py` — validates manifest/arrangement/song_timeline payloads against
  the vendored feedpak-spec schemas, always returning a report rather than raising.

`routes.py` loads its siblings via `context['load_sibling']` (not bare imports) to avoid
the plugin-module-name collisions that mechanism exists to prevent.

## Tests

```bash
pip install jsonschema
pytest
```

`tests/test_pipeline.py` needs the feedBack host's `lib/` (for `guitarpro`, `gp2rs`,
`song`, `gp2midi`) on `sys.path` — `tests/conftest.py` looks for a sibling
`pakr/feedBack` checkout and self-skips those tests if it isn't found. It also includes
a regression test locking in the fix for "Money (J).gp5" — historically documented as
losing all 16 of its section markers — extracting all 16 correctly.

## License

MIT
