# Skipped tests: inventory and policy

`python -m pytest -ra` (the default via `pytest.ini`) prints the short test
summary, so every skip and its reason is visible in the log. This file records
*why* each category exists and whether it is allowed to skip in CI.

The rule from issue #81: a skip is only acceptable when the missing thing is
genuinely optional. Anything that gates a **release regression** must be
provisioned in CI and enforced there, not skipped.

CI sets `FEEDPAKR_REQUIRE=host,ffmpeg` (`.github/workflows/tests.yml`). Two
guards in `tests/conftest.py` enforce the rule, so a green run means the
required regressions really executed:

- `pytest_configure` **aborts the whole session** (exit 1) when a required
  capability is missing at startup (host lib not importable, `ffmpeg` not on
  `PATH`).
- `pytest_sessionfinish` **fails the session** if any test still skipped for a
  reason not tolerated for the required capabilities. Tolerated reasons are the
  optional categories below plus, per capability, skips that signal *that*
  capability's absence (`_support.CAPABILITY_SKIP_SUBSTRINGS`); an unrecognized
  skip reason fails the run so it gets reviewed rather than hiding. Both are
  no-ops when `FEEDPAKR_REQUIRE` is unset.

## Categories

### 1. Host core lib missing — required in CI

Reason: `feedBack host core lib not on sys.path` (and the equivalent
`pyguitarpro not installed` for a bare import).

Files: `test_pipeline.py`, `test_real_tabs.py`, `test_portable_conversion.py`,
`test_ci_gpif_fixture.py`, `test_fidelity_report.py`, `test_tones.py`.

`tests/conftest.py` adds a sibling `pakr/feedBack/lib` checkout to `sys.path`
when present. CI checks that checkout out (`.github/workflows/tests.yml`) and
includes `host` in `FEEDPAKR_REQUIRE`; `tests/conftest.py::pytest_configure`
then **aborts the whole session** if the lib is not importable, so these tests
can never silently skip in CI. Locally, without the sibling checkout, they skip
and everything else still runs.

### 2. FFmpeg missing — required in CI

Reason: `ffmpeg not installed on this host (optional)` and the runtime
`ffmpeg cannot generate a test tone: ...` guard.

Files: `test_audio.py`, `test_upgrade.py`.

These drive the real mixdown / transcode / auto-sync regressions, which are
release-critical. CI installs the `ffmpeg` package (which also provides
`ffprobe`) and includes `ffmpeg` in `FEEDPAKR_REQUIRE`, so a missing binary
aborts the session. The residual skips only fire locally or on an ffmpeg build
genuinely lacking the `lavfi` `sine` filter / Ogg encoder (a real capability
gap, not a silent pass).

### 3. Optional personal sample library — never required

Reason: `optional personal sample library not configured
(set FEEDPAKR_LOCAL_FIXTURES=/path/to/samples)`.

Files: `test_real_tabs.py`, `test_pipeline.py` (Money GP5/GP8, capo fixture),
`test_upgrade.py` (sample sloppak library).

These lock in behavior observed against real, copyrighted songs and sloppaks
that cannot be committed. They are **optional by design** and are not expected
to run in CI. The equivalent *behaviors* are covered portably — see
`test_portable_conversion.py` and the fixtures in `portable_fixtures.py`:

| Optional test (real sample)        | Portable coverage                                |
| ---------------------------------- | ------------------------------------------------ |
| `test_real_tab_sections_ordered_and_named` | `test_gpif_sections_ordered_and_named`, `test_gp345_sections_ordered_and_named` |
| `test_real_tab_chords_map_consistently_to_templates` | `test_gpif_chord_occurrences_match_their_templates`, `test_gp345_chord_occurrences_match_their_templates` |
| `test_build_feedpak_gp345_drums_become_type_drums_arrangement` | `test_gp345_drums_become_drum_tab` |
| GPIF-vs-GP3-5 format/tuning handling | `test_gpif_tuning_reads_absolute_pitches_as_offsets`, `test_format_detection_differs_between_gpif_and_gp345` |

Point `FEEDPAKR_LOCAL_FIXTURES` at a directory laid out like the historical
default (`tabs/`, `export/`, the Money files at the root) to run the optional
suite locally. With no env var, the historical path is used if it exists.

### 4. Optional Python dependencies

Reasons: `jsonschema not installed` (`test_validate.py`), `pyguitarpro not
installed` / `song module not on sys.path` / `gp_autosync not on sys.path`.

All are installed in CI by `requirements-test.txt` / the host
`requirements.txt`, so they do not skip there.

### 5. Intentional, narrow skips

- `test_tones.py::...` — the upstream pyguitarpro bug where a synthetic
  in-memory `Song` has no attachable beats in this version; the test can't
  construct its subject. Guarded so other pyguitarpro versions still run it.
- `test_real_tabs.py::test_real_tab_warns_about_same_string_chord_collisions` —
  only `So Far Away (J).gp` is a *known* trigger of the same-string
  chord-template host gap; other fixtures skip as `not a known-affected
  fixture`. The warning emission itself is still exercised for the affected
  file when the optional library is present.
