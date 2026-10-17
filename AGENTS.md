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

## Repository & PR target

This is the canonical `get-flashbacks/feedBack-plugin-feedpakr` repository. All changes should be opened as PRs against `get-flashbacks/feedBack-plugin-feedpakr` (default branch `main`). It is not a fork of another repo — `got-feedBack`'s plugin ecosystem (`feedpak-spec` and `feedBack-plugin-spec`) provides the specifications this plugin follows. Refer to those specs for normative requirements; host implementation details are noted where they differ.

## Agent completion workflow

Follow this single completion workflow for all agent tasks:

1. **Understand the task**: Read the task requirements and related context (issue, PR, or instructions). Check AGENTS.md for repo-specific guidance; for detailed architecture notes (audio/chart sync, Chordr, dedupe) consult `CLAUDE.md`, and for test/setup detail see `README.md`.
2. **Plan**: Identify affected files, key changes, and verification steps. Create a concise todo list if the work spans multiple steps.
3. **Implement**: Make minimal, focused changes that follow existing code style, naming, and patterns. Use `context['load_sibling']` for cross-module imports, `context['log']` for logging, and respect the plugin's architecture (blocking work in plain `def` handlers).
4. **Test**: Run the test suite with `pytest` to catch regressions. Verify changes against the relevant specs when touching manifest shape or validation logic.
5. **Lint/format**: Follow repo conventions; no separate lint command is mandated beyond what tests/checks expect (see learnings). Ensure code matches surrounding style.
6. **Verify**: Confirm the implementation meets acceptance criteria from the task/issue. Run any targeted checks relevant to your changes.
7. **Finalize**: Remove stray artifacts (test/tooling output) so only the task's intended changes remain, then commit and push those edits as your harness requires — never revert a requested change to satisfy a clean-tree check. Summarize changes and follow the run's reporting requirements. If the task involves creating a PR, ensure it targets `get-flashbacks/feedBack-plugin-feedpakr` (default `main`).

## Reproducible setup

- **Dependencies**: `pip install -r requirements.txt -r requirements-test.txt` covers this plugin's own deps, but the host libraries (`pyguitarpro`, `PyYAML`) are deliberately not declared there. `tests/conftest.py` imports `feedpakr_pipeline` → `feedpakr_pack`, whose module-scope `import yaml` aborts collection without `PyYAML`, so install them (`pip install pyguitarpro PyYAML`) or add the host's requirements (`-r ../pakr/feedBack/requirements.txt`, which needs a sibling host checkout). See `README.md`'s Tests section.
- **Test command**: `FEEDPAKR_REQUIRE=host,ffmpeg python -m pytest` from repo root, with the host `lib/` on `sys.path` (a sibling `pakr/feedBack` checkout or `PYTHONPATH`) and `ffmpeg` on `PATH` (imageio-ffmpeg can supply a binary locally). A bare `pytest` leaves `FEEDPAKR_REQUIRE` unset, so host/ffmpeg regression tests silently self-skip and the exit code proves nothing — setting the requirement turns those skips into a failure. See [`tests/SKIPS.md`](tests/SKIPS.md) for the skip policy.
- **Specs**: the normative specs are the upstream `got-feedBack` repos [`feedpak-spec`](https://github.com/got-feedBack/feedpak-spec) and [`feedBack-plugin-spec`](https://github.com/got-feedBack/feedBack-plugin-spec); neither is vendored here. This repo ships only the `assets/schemas/*.json` schemas derived from `feedpak-spec`, so consult the upstream repos for the full normative text — local-looking paths below such as `feedpak-spec/tools/validate.py` denote files within those upstream repos, not a checkout here.

## Verification

- **Tests**: Always run `pytest` after making changes. Pay attention to skip counts — a green exit with all skips means environment lacks fixtures/host deps.
- **Validation**: When modifying manifest assembly or validation, verify against `feedpak-spec/tools/validate.py` or vendored schemas via `feedpakr_validate.py`.
- **Behavior**: For complex changes (sync/warp, audio, GP parsing), add/adjust tests or do targeted manual checks against existing fixtures.

## Optional integrations

- **Chordr**: Optional chord naming during import; failures degrade gracefully and emit warnings.
- **FFmpeg**: Required by certain audio operations. Its tests self-skip locally when `ffmpeg` isn't on `PATH`, but CI treats it as required (`FEEDPAKR_REQUIRE=host,ffmpeg`) and aborts the session if it's missing, so a broken provisioning step fails loudly instead of hiding behind skips.

## Versioning

Bump `version` in `plugin.json` for **every** functional source change — any `.py`/`.js`/`.html`/`.css` file or schema JSON. CI's `version-bumped-on-change` check enforces this on every PR and fails when the version is unchanged from the base branch, so "not user-visible" is not an exemption (the version also cache-busts the served JS/CSS URL, so an unbumped release serves stale files). Docs, tests, fixtures, `.github/`, and a version-only bump are exempt. Patch (`0.x.y` with `0.x` series) for fixes, minor (`0.x.0`) for new features, matching semver during 0.x. This is independent of `feedpak_version` (the *format* version) — don't conflate the two.

## Changelog policy

Record every functional source change in `CHANGELOG.md` following [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) — CI's `changelog-updated` check requires a `CHANGELOG.md` diff on any PR that changes a functional path (the same set as the version bump), so "notable" is not a judgment call. Add entries under Added/Changed/Fixed/Removed/Security and link or reference issues/PRs as appropriate.

## Completion criteria

- All tests pass (or expected skips are documented and justified)
- Changes meet acceptance criteria in the task/issue
- Code follows existing conventions and plugin/feedpak specs
- Working tree contains only the task's intended changes (stray test/tooling artifacts removed); requested edits are committed/pushed, never reverted