# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- Add entries under Added, Changed, Deprecated, Removed, Fixed, or Security as changes land. -->

### Fixed

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
