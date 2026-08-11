# feedpakr — AI Agent Guide

Imports Guitar Pro files (GP3-GP8) into the `.feedpak` package format, and
upgrades existing `.sloppak` files to `.feedpak`. Backend-heavy: most of
the real work (`feedpakr_pipeline.build_feedpak`) runs off the request
thread via `run_in_executor`, streamed to the client over
`/ws/plugins/feedpakr/build`.

## Plugin-spec compliance (see got-feedBack/feedBack-plugin-spec)

- **Folder name must equal `plugin.json`'s `id` exactly** (case-sensitive)
  — a mismatch is a silent skip at discovery, the most common "why won't
  it load" cause.
- **`setup(app, context)` must stay fast and side-effect-free at import
  time** — it only registers routes; GP parsing/conversion happens inside
  request handlers (or their executor threads), never during plugin load.
- **Sibling modules load via `context['load_sibling'](...)`**, not bare
  `import` — this repo already does this for `feedpakr_pipeline`,
  `feedpakr_pack`, `feedpakr_audio`, `feedpakr_upgrade`. Keep new
  cross-file code on this path; a bare `import` risks `sys.modules`
  collisions with another plugin's same-named module.
- **Log via `context['log']`, never `print()`.** Already followed
  throughout `routes.py` — keep it that way.
- **Routes are namespaced under `/api/plugins/feedpakr/...`** (and the
  build websocket under `/ws/plugins/feedpakr/...`). Don't introduce a
  route outside that prefix.
- **Blocking work in a route handler should be plain `def`, not `async
  def`**, so the Host's threadpool actually parallelizes it — see how
  `_do_build()` inside `ws_build` is a plain function handed to
  `run_in_executor`, not awaited inline.

## Audio/chart sync — flat offset vs. tempo-aware warp, and manual override

`build_feedpak` resolves audio via `_resolve_audio()` **before** calling
`gp2rs.convert_file(...)` — an offset computed in `_resolve_audio` has
nowhere else to go (no manifest-level offset field exists anywhere in this
pipeline), so it must be baked into the XML before `song_mod.parse_arrangement`
/`song_mod.load_song` read it back. **This "resolve-then-convert" ordering
was itself a bug fix** (commit `2f77b03`, 2026-07-31) — before it, the
offset was computed after `convert_file` and silently discarded for
`embedded`/`sync` modes. `existing_pack` mode (`e6e2e7b`) was built on top
of the already-fixed ordering.

`_resolve_audio()` returns **three** values now: `(audio_path, offset,
sync_points)`. For `audio_mode in {'sync', 'existing_pack'}` it either:
- runs `feedpakr_audio.autosync_audio()` (chroma-CQT + DTW against the
  tab's synthesized pitch content, refined with an onset phase sweep — see
  `lib/gp_autosync.py` in the host repo), which returns both a scalar
  `audio_offset` (bar-1 alignment) **and** a list of per-bar `sync_points`
  sampled along the DTW path, or
- if the caller passed `manual_offset` (a plain float, seconds — see
  below), skips autosync entirely and uses that number as `offset` with
  `sync_points = []`.

**Tempo-aware warp, not just a flat offset.** A single scalar offset only
holds the chart in sync at bar 1 — the module docstring on
`gp_autosync.py` says it outright: *"Applying only the scalar audio_offset
(bar 1) assumes the recording holds the authored tempo for the whole song
— any drift accumulates."* This used to be exactly what happened: the
per-bar `sync_points` from `autosync_audio()` were computed and then discarded
(`offset, _points, err = audio_mod.autosync_audio(...)`), so any real
recording whose tempo didn't track the GP file's authored tempo map
exactly (a live take, natural rubato) would drift out of sync as the song
progressed. Fixed: `build_feedpak` now calls `_build_warp_fn(gp_path,
sync_points, warnings)`, which turns `sync_points` into a piecewise-linear
score-time → audio-time mapping via `gp_autosync.build_warp_anchors()` +
`warp_time()` (falls back to `None` — the old flat-offset behavior — when
there are fewer than 2 usable anchors, `gp_autosync` isn't importable, or
`sync_points` is empty because manual/non-autosync mode was used). When a
`warp_fn` is available:
- `convert_file(..., audio_offset=0.0)` — chart is written as-written, no
  flat shift baked in;
- each arrangement's parsed `Arrangement` (`arr`) is warped in place via
  `_warp_arrangement(arr, warp_fn)` (wraps `gp_autosync.warp_song_times`
  with a throwaway single-arrangement `Song`) **before**
  `arrangement_to_wire(arr)` — covers notes/chords/anchors/hand_shapes/phrases;
  `_gpif_drumtab_from_wire` inherits correct hit times for free since it
  reads the already-warped `wire`;
- `song_meta` (the `Song` used for `song_timeline`/`duration`) is warped
  the same way right after `_load_song_meta`, before `_song_timeline_from_meta`
  and before `duration` is read — GP3-5's `extract_gp345_lyrics()` and
  `extract_gpif_keys()`/`extract_gp345_keys()`, which take
  `song_timeline['beats']` as an input, inherit correct timing for free
  since they run after this;
- GPIF's `<vocals>` XML path is the one exception that does **not** inherit
  timing for free: it's read straight off `xml_path` in the
  `is_vocals_xml()` branch (`parse_vocals_xml`/`parse_vocal_pitch_xml`),
  which `continue`s before reaching the fretted-track warp code below — so
  it gets its own explicit per-entry warp pass right there (`t` and `d`,
  end-start like every other duration field) when `warp_fn` is active.
  Missing this was a real regression caught in review (PR #45): GPIF
  vocal timing would have silently kept reading as-written XML while every
  other arrangement type in the same build was warped;
- the GP3-5 **native** drum-tab path (`gp2rs.convert_drum_track_to_drumtab`)
  doesn't go through `convert_file`/`arr` at all, so its hits are warped
  by a manual per-hit loop after the call. **This call was also missing
  `audio_offset` entirely before this fix** — a real, standalone bug (every
  other arrangement got the flat sync shift, GP3-5 native drum arrangements
  got none) — now it gets either the flat offset or 0.0+per-hit-warp,
  matching every other arrangement type.
- the duration sanity-check's `aligned_duration` no longer double-adds
  `audio_offset` when `warp_fn` is active, since `song_meta.song_length`
  is already expressed in audio-time after the warp.

**Deliberately out of scope:** `wire['tones']` (tone-change markers) are
extracted straight from `gp_song`/`gp_path`/`xml_path` by
`feedpakr_tones.py` with no offset parameter at all, for *both* GP3-5 and
GPIF — they've never been offset-corrected by this pipeline, warp or no
warp. Left alone here rather than scope-creeped in; a real fix needs
`feedpakr_tones.py`'s extractors to accept an offset/warp themselves.
Notation (`feedpakr_notation.py`) reads straight from `gp_path` too and is
similarly never offset-corrected — same story, separate pre-existing gap.

`result['features']['tempo_aware_sync']` reports whether the warp was
actually used for a given build (`False` for manual offset, non-autosync
modes, or a degraded/failed autosync run) — useful for tests/diagnostics
without re-deriving it from `warnings` text matching.

**Manual sync override.** `build_feedpak(..., manual_offset: float | None)`
plumbs through from `routes.py`'s `ws_build` (`manual_offset: str = ''`
query param, parsed to float or rejected with an error) and from the UI
(`screen.html`'s `#fpr-sync-method-controls` radio pair + `#fpr-manual-offset`
number input, shown whenever `audio_mode` is `sync`/`existing_pack`; collected
in `screen.js`'s `fprBuild()`). Use it when autosync's DTW alignment picks
the wrong spot — it bypasses autosync (and therefore the warp) entirely
and applies the given number of seconds as a flat shift, the same way the
pre-fix code always worked. Both ends validate it's actually a *finite*
number — `float()` happily parses `'inf'`/`'Infinity'`/`'nan'`/`'1e999'`,
so `routes.py`'s `ws_build` checks `math.isfinite()` (not just a bare
`try/except ValueError`) and `screen.js` uses `Number.isFinite`, not
`!Number.isNaN`, in `fprCollectManualOffset` — a non-finite offset would
otherwise flow straight into the chart as a literal `Infinity`/`NaN`
timestamp and produce a spec-invalid pack.

**GP3-5 repeats vs. the warp: gated, not silently wrong.**
`gp_autosync.gp_has_expandable_repeats()` (checks for repeat
brackets/voltas/D.S./D.C. in a GP3/4/5 file) exists specifically because
`bar_start_times()`/`auto_sync()` anchor the warp on the **as-written**
score (each repeated bar sampled once), while `convert_file`'s default
`expand_repeats=True` emits an **as-performed** timeline (repeats played
out in full) — a warp built from the former mis-maps every repeat pass
past the first (later passes fall outside the anchor range and get
extrapolated with the last segment's slope). `build_feedpak` now calls it
right after building `warp_fn`: if the file has expandable repeats (GP3-5
only — GPIF's own repeat handling is as-written on both sides, so
`gp_has_expandable_repeats()` is hardcoded `False` there), the warp is
discarded in favor of the flat offset these files always got, with a
warning explaining why — falling back to a known-safe behavior instead of
a warp that's likely wrong for everything after the first repeat. GPIF's
*different* repeat limitation (`_gpif_has_repeat_markup` → the
"repeats/alternate endings... GP6/7/8 import does not yet expand" warning)
is unrelated and unaffected by this.

## feedpak-spec compliance (see got-feedBack/feedpak-spec)

- **Manifest required keys:** `title`, `artist`, `duration`,
  `arrangements[]` (non-empty), `stems[]` (non-empty). Everything else
  `assemble_manifest` writes (`album`, `year`, `album_artist`, `track`,
  `disc`, `genres`, `mbid`, `isrc`, `language`, `authors`, `cover`,
  `lyrics*`, `keys`, `vocal_pitch`, `song_timeline`) is optional —
  `assemble_manifest` already follows the spec's "only emit keys you
  actually fill" convention; keep new fields on that same pattern rather
  than writing empty/null placeholders.
- **An empty `stems[]` is spec-legal** (the §5.3.2 "authoring
  intermediate" carve-out for a pack built with `audio_mode='none'`) but
  won't pass strict validation until audio is added — that's surfaced as
  a pipeline warning, not hidden.
- **`feedpak_version`** should track the spec version this pack targets
  (`FEEDPAK_VERSION` in `feedpakr_pack.py`); bump it only when actually
  adopting a newer spec revision's keys, not casually.
- Validate real changes against `feedpak-spec/tools/validate.py` (or the
  JSON Schemas under `feedpak-spec/schemas/`) when touching manifest
  shape — `feedpakr_validate.py` wraps this for the pipeline's own
  post-build check.

## Versioning

Bump `version` in `plugin.json` whenever a change is user-visible — new
import capability, a fixed bug that affected real output, a changed UI
flow (best-practices rule 4: bump on every release, the plugin manager
uses this to detect updates). It was left at `0.1.0` from the very first
commit through 9 subsequent commits (four full feature phases) before
anyone noticed — don't let it go stale again. Patch (`0.1.x`) for fixes,
minor (`0.x.0`) for new features, matching normal semver-during-0.x
conventions. This is independent of `feedpak_version` (the *format*
version) — don't conflate the two.
