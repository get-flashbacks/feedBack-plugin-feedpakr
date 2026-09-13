# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- Add entries under Added, Changed, Deprecated, Removed, Fixed, or Security as changes land. -->

### Added

- **Duplicate `.feedpak` cleanup** (issue #49). The Upgrade Library tab
  can now scan the DLC folder for `.feedpak` files with byte-identical
  archive content — e.g. the numbered copies (`Song_2.feedpak`, …) left
  behind by re-upgrading before `conflict_policy` existed — and remove
  caller-selected duplicates after explicit review and confirmation.
  Detection hashes actual member content, not file bytes/size/mtime, so
  two packs built from the same source at different times (different zip
  timestamps) are still correctly recognized as identical. Removal is
  recoverable: files are moved into a `.feedpakr_trash/` folder inside the
  DLC directory rather than deleted outright, and at least one copy per
  duplicate group is always kept even if every member is selected. Never
  touches `.sloppak` sources, and every safety check (extension, DLC-root
  containment, still-a-real-duplicate, not-the-last-copy) is re-verified
  at delete time against a fresh scan, not whatever the UI last saw.

### Fixed

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
