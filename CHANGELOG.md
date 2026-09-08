# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- Add entries under Added, Changed, Deprecated, Removed, Fixed, or Security as changes land. -->

### Fixed

- `existing_pack` builds could produce a manifest with no stem marked
  `default: true` (or no `full` stem entry at all) when the only full
  mixdown came through `extra_stems` rather than `stem_file`, leaving the
  pack unplayable. `assemble_manifest()` now backfills `default: true`
  onto whichever stem has `id: full` regardless of which parameter
  supplied it, dedupes a duplicate `full` entry, and strips any stray
  `default` from non-full entries. (#44)
