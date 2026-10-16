# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.3] - 2026-10-11

<!-- Add entries under Added, Changed, Deprecated, Removed, Fixed, or Security as changes land. -->

### Added

- **Optional Chordr chord naming during Guitar Pro imports.** When Chordr is
  installed, the import form can use its shared chart-analysis bridge to name
  unnamed chord templates from played voicings and partial strums. Existing
  chart names are retained; unavailable or failed analysis leaves the build
  intact and reports a warning.

- **Chordr naming skips piano/Keys arrangements.** Their wire encoding uses
  MIDI pitches rather than fretted positions, so passing those notes to
  Chordr could assign incorrect chord names.

- **Duplicate `.feedpak` cleanup** (issue #49). The Upgrade Library tab
  can now scan the DLC folder for `.feedpak` files with byte-identical
  archive content — e.g. the numbered copies (`Song_2.feedpak`, …) left
  behind by re-upgrading before `conflict_policy` existed — and remove
  caller-selected duplicates after explicit review and confirmation.
  Detection hashes actual member content, not file bytes/size/mtime, so
  two packs built from the same source at different times (different zip
  timestamps) are still correctly recognized as identical. A corrupt,
  encrypted, or otherwise unreadable member (or a symlink pointing outside
  the DLC folder) is skipped rather than aborting the whole scan. Removal
  is recoverable: files are moved
  into a `.feedpakr_trash/` folder inside the DLC directory rather than
  deleted outright, with a collision-free destination name even under
  concurrent requests. Each group's **oldest** file — the one
  `already_upgraded` detection keys off — is always protected and can
  never be removed via this flow, even by selecting every member of a
  group at once; its checkbox is shown disabled in the UI, and the
  backend refuses it independently regardless of what the UI sends.
  Never touches `.sloppak` sources, and every other safety check
  (extension, DLC-root containment, still-a-real-duplicate) is re-verified
  at delete time against a fresh scan, not whatever the UI last saw.

### Fixed

- **An invalid `notation_tracks` value made the upload unrecoverable.**
  `ws_build` popped the single-use upload token before parsing
  `notation_tracks`, so a bad value returned its error only after the
  token was already gone: the caller couldn't retry with the same upload,
  and its temp dir was orphaned because the TTL purger only visits
  registered uploads. Parameter validation now runs before the token is
  consumed. (#75)

- **Concurrent builds with the same song could overwrite each other's
  output.** `unique_output_path` picked a name by checking existence and
  returning it without reserving anything, so two builds (or keep-both
  upgrades) racing on the same sanitized title/artist could both select
  the same `.feedpak`, both report success, and one write silently
  clobber the other's. Each output name is now reserved for the duration of
  its write, so concurrent writers always land on distinct names — for both
  fresh builds and versioned keep-both upgrades. Reservations are held in
  memory (the writers are threads in the one host process), so an interrupted
  writer leaves no on-disk marker behind to squat the unnumbered name and push
  later upgrades onto a numbered copy `list_sloppaks` never recognizes. (#74)

- **The Upgrade Library WebSocket accepted any path, not just
  `.sloppak`.** A request naming a `.feedpak` (or any other file) would
  reach `upgrade_sloppak()`, which was never designed to read it. Paths
  are now validated to end in `.sloppak` (case-insensitive) up front —
  the whole batch is rejected with a clear error if any entry doesn't
  match, before any conversion work starts. (issue #49)

- **Re-upgrading an already-upgraded `.sloppak` silently created a
  numbered duplicate `.feedpak`** (`Song_2.feedpak`, `Song_3.feedpak`, …)
  instead of asking what to do. The Upgrade Library batch route now takes
  a `conflict_policy` (skip / replace / versioned — versioned matches the
  old default behavior exactly, so nothing changes unless a file is
  explicitly re-selected). The UI now shows a one-time choice — applied
  to that run only, never saved as a sticky default — whenever the
  current selection includes a file already flagged `already_upgraded`.
  (issue #49)

- `existing_pack` builds whose source pack had only separated stems and
  no combined mixdown produced a manifest with no `full` stem entry and
  therefore no default-playable stem, with no warning surfaced (unlike
  the fully-audio-less authoring-intermediate case, which already
  warns). Now warns the same way. `assemble_manifest()` also now
  backfills `default: true` onto whichever stem has `id: full` regardless
  of whether it came via `stem_file` or `extra_stems`, and dedupes
  duplicate `full` entries (including duplicates within `extra_stems`
  itself) so at most one stem is ever marked default. (#44)
- `fprCollectManualOffset()`'s client-side validation for the manual
  audio-sync offset used `Number.isFinite(Number(raw))`, which accepts
  hex/octal/binary literals (e.g. `"0x10"`) as finite decimal values —
  but `routes.py`'s `ws_build` parses the same string with Python's
  `float()`, which rejects all three forms outright. A user who typed
  such a value saw no client-side error; the build request went out and
  only then came back the server's "manual_offset must be a finite
  number of seconds" error, after the progress UI had already started.
  Added a regex matching the plain-decimal grammar `float()` actually
  accepts, so client and server now agree on every case.
- The manual-offset regex above initially only accepted plain digits,
  which is *stricter* than `float()` in the opposite direction: Python's
  `float()` also accepts PEP 515 underscore-grouped digits (`"1_000"`,
  `"1_2.5"`), which the regex rejected outright — blocking a value the
  server would have parsed and used successfully. Widened the regex to
  accept underscores between digits (matching Python's own placement
  rule: one digit required on each side of every underscore).
- `manual_offset` was validated for finiteness (rejecting `Infinity`/`NaN`)
  but not plausibility — a finite value larger in magnitude than the
  song itself (e.g. `-3600` on a 3-minute song) produced a spec-valid,
  unplayable pack with no warning. `build_feedpak` now warns when
  `abs(manual_offset) > duration`. (#54)

## [Unreleased]

### Changed

- **CI policy checks for functional changes are now structural, not
  text-presence** (issue #84). The `blocking-handlers-use-def` and
  `idempotent-top-level-guard` checks previously passed on any file that
  merely contained a marker string; they now parse `routes.py` (AST) and
  `screen.js` for the actual call sites and fail on unwrapped blocking work
  in async handlers or unguarded top-level listeners. The functional-change
  policy now classifies CSS, schema, and non-`version` `plugin.json` edits
  as functional (with documented exemptions for tests, CI, and docs), and
  is shared between the version and changelog jobs via
  `.github/scripts/custom_checks.py` so both agree. Remaining text-presence
  checks are labelled as heuristics, and the modified checks have
  representative pass/fail cases in `tests/test_custom_checks.py`.

### Added

- **Declared and probed host-core compatibility** (issue #66). feedpakr's
  host requirements — the core Python modules/helpers each feature calls,
  the external binaries (FFmpeg/ffprobe, FluidSynth, yt-dlp) and the
  optional sibling plugins it hands off to — are now declared in one place
  (`feedpakr_compat.py`) and probed at runtime, exposed via
  `GET /api/plugins/feedpakr/compat` and surfaced in the UI as a banner
  when a feature is unavailable or running on a fallback. The tested host
  baseline is pinned to the exact commit
  `d876ded00f9254b13bf4a0587c5645a1edaccf67`: the host's `VERSION` string
  (`0.3.0-alpha.1`) is reused across checkouts that do and don't have
  `read_member_bytes()`, so it can't identify the baseline on its own.
  `plugin.json` gains an advisory `minHost`.

### Fixed

- **Upgrading on a host whose `sloppak` module predates
  `read_member_bytes()` raised `AttributeError` instead of degrading.**
  `feedpakr_upgrade._read_member` called `read_member_bytes` whenever the
  `sloppak` module imported successfully, so an older host — the exact gap
  the compatibility baseline exists to describe — failed the upgrade rather
  than falling back to feedpakr's own bounded reader. Helper reads now check
  for the attribute explicitly and fall back
  (`feedpakr_upgrade._sloppak_has`); `load_manifest` gets the same
  treatment. The `/lyrics-text` and `/sloppaks` routes read through
  `feedpakr_upgrade`'s guarded readers instead of calling `sloppak`
  directly, so they degrade the same way.

