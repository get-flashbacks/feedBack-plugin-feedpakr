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
