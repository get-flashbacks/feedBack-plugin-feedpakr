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

## Audio/chart sync — how it works and its known gap

`build_feedpak` resolves audio (and a single scalar `audio_offset`) via
`_resolve_audio()` **before** calling `gp2rs.convert_file(...,
audio_offset=audio_offset)` (`feedpakr_pipeline.py` around line 725, with
a comment explaining why the ordering matters). `convert_file` adds that
one offset to every note/beat/section/anchor time uniformly (see
`gp2rs.py`/`gp2rs_gpx.py` — every `RsBeat`/`RsSection`/note timestamp gets
`+ audio_offset`). There is no separate manifest-level offset field
anywhere in this pipeline — an offset computed in `_resolve_audio` has
nowhere else to go, so it must be baked into the XML before
`song_mod.parse_arrangement`/`song_mod.load_song` read it back. **This
"resolve-then-convert" ordering was itself a bug fix** (commit `2f77b03`,
2026-07-31) — before it, the offset was computed after `convert_file` and
silently discarded for `embedded`/`sync` modes. `existing_pack` mode
(added later, `e6e2e7b`) was built on top of the already-fixed ordering,
so it does not have that particular bug.

**The gap that's still there:** for `audio_mode in {'sync',
'existing_pack'}`, the offset comes from `feedpakr_audio.autosync_audio()`
→ core's `gp_autosync.auto_sync()` (chroma-CQT + DTW against the tab's
synthesized pitch content, refined with an onset phase sweep — see
`lib/gp_autosync.py` in the host repo). `auto_sync()` returns **both** a
scalar `audio_offset` (bar-1 alignment) **and** a full list of per-bar
`sync_points` sampled along the DTW path — the latter is exactly what
`gp_autosync.build_warp_anchors()` / `warp_time()` / `warp_song_times()`
exist to consume, turning it into a piecewise-linear score-time →
audio-time mapping so the chart follows the recording's actual tempo
bar-by-bar (the module docstring literally says: *"Applying only the
scalar audio_offset (bar 1) assumes the recording holds the authored
tempo for the whole song — any drift accumulates"*).

`feedpakr_audio.autosync_audio()` throws the `sync_points` away —
`feedpakr_pipeline.py` binds the return as `offset, _points, err =
audio_mod.autosync_audio(...)` in **both** the `'sync'` (line ~615) and
`'existing_pack'` (line ~632) branches of `_resolve_audio`. Only the
scalar `audio_offset` is used, applied uniformly via `convert_file`. So:
whenever the real audio's tempo doesn't track the GP file's authored
tempo map exactly — a live recording, a human performance, any take with
natural rubato — the chart and audio start in sync at bar 1 and drift
apart as the song progresses, worst near the end. This is the most likely
explanation if you're chasing an "arrangements are unsynchronized from
the audio" report for GP re-imports (`'existing_pack'` mode is
specifically the path that re-fits a *previously recorded* pack's audio,
where tempo mismatch is common). The `/autosync-preview` UI route
(`routes.py`) does return `sync_points` to the frontend, but only for
display in the preview — the actual `ws_build` path never sees them.

A real fix would parse arrangements/song timeline with `audio_offset=0`,
then call `gp_autosync.warp_song_times(song, warp_fn)` — where
`warp_fn = lambda t: gp_autosync.warp_time(t, anchors)` and `anchors =
gp_autosync.build_warp_anchors(sync_points, gp_autosync.bar_start_times(gp_path))`
— on the parsed `Song`/`Arrangement` objects before re-serializing to
wire format, instead of baking a flat offset into the XML up front. Not
done yet; flagging here so it isn't re-discovered from scratch.

Separately, `gp_autosync.gp_has_expandable_repeats()` exists (checks for
repeat brackets/voltas/D.S./D.C. in a GP3/4/5 file — files where
`convert_file`'s as-performed, repeat-expanded output diverges from the
as-written score `auto_sync` aligns against) but **nothing calls it** —
not `feedpakr_pipeline.py`, not `gp_autosync.py` itself outside its own
definition. GPIF (GP6/7/8) imports already surface an equivalent warning
for their own (different) repeat-expansion limitation
(`_gpif_has_repeat_markup` → the "repeats/alternate endings... GP6/7/8
import does not yet expand" warning in `build_feedpak`); GP3-5 has no
analogous warning even though the underlying function to detect the
condition already exists.

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
