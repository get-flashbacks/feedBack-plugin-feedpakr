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
