"""Declared feedBack-core compatibility for feedpakr's features.

feedpakr is a plugin, not a fork. It calls the host's own Python helpers
(``gp2rs`` / ``song`` / ``sloppak`` / …) instead of vendoring them, and those
helpers change on their own schedule. So "which host can run which feature"
has to be declared and *checked* rather than assumed.

This module is the single source of truth for that declaration:

  - ``CORE_BASELINE_*`` — the exact host commit feedpakr's full toolset was
    last exercised against. The host's version string is not a unique
    identifier (``VERSION`` reads ``0.3.0-alpha.1`` both at the baseline
    commit and later), so the baseline is pinned by commit, per the core
    compatibility issue.
  - ``FEATURES`` — each user-facing operation with the core modules/helpers
    it needs, kept deliberately separate from the external binaries
    (ffmpeg, FluidSynth, yt-dlp) and the optional sibling plugins it may hand
    off to. A missing binary or sibling is *not* a core-version problem and
    must not be reported as one.
  - ``compatibility_report()`` — probes the running host and reports, per
    feature, whether it is available, degraded (a compatible fallback covers
    a missing helper), or unavailable, with an actionable message.
"""

from __future__ import annotations

import importlib
import importlib.util
import shutil
from dataclasses import dataclass


# ── Tested core baseline ──────────────────────────────────────────────────
#
# Exact commit, not a version tag: the feedBack ``VERSION`` file reads
# ``0.3.0-alpha.1`` at this commit and still reads the same on later
# checkouts, so a semver comparison cannot distinguish a host that has the
# helpers below from one that doesn't. Bump this only after re-testing the
# full toolset against a newer host.
CORE_BASELINE_REPO = 'got-feedBack/feedBack'
CORE_BASELINE_COMMIT = 'd876ded00f9254b13bf4a0587c5645a1edaccf67'
CORE_BASELINE_COMMIT_SHORT = CORE_BASELINE_COMMIT[:7]
CORE_BASELINE_VERSION = '0.3.0-alpha.1'
CORE_BASELINE_NOTE = (
    'feedpakr\'s full toolset (GP import, existing-pack reuse, upgrade, '
    'duplicate scan, output playback) was last tested against this commit. '
    'The version string is advisory only: the same VERSION value appears on '
    'hosts both with and without read_member_bytes().'
)


# ── Declarations ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CoreHelper:
    """A single attribute feedpakr needs from a host core module.

    ``fallback`` is a human-readable description of feedpakr's own
    compatible replacement, or None when the helper is a hard requirement
    (its absence must disable the dependent feature, not degrade it).
    """

    module: str
    attr: str
    landed: str
    fallback: str | None = None

    @property
    def has_fallback(self) -> bool:
        return self.fallback is not None

    @property
    def key(self) -> str:
        return f'{self.module}.{self.attr}'


@dataclass(frozen=True)
class ExternalTool:
    """A non-core runtime dependency (binary or pip module).

    Kept out of the core-version metadata on purpose: FFmpeg/FluidSynth/
    yt-dlp have their own release cycles and say nothing about whether the
    host's feedBack core is new enough.
    """

    label: str
    purpose: str
    binary: str | None = None
    module: str | None = None


@dataclass(frozen=True)
class Feature:
    """One user-facing operation and everything it depends on."""

    key: str
    label: str
    description: str
    core_modules: tuple[str, ...] = ()
    optional_core_modules: tuple[str, ...] = ()
    core_helpers: tuple[CoreHelper, ...] = ()
    external_tools: tuple[str, ...] = ()
    optional_siblings: tuple[str, ...] = ()
    probed: bool = True
    note: str = ''


# Host core helpers feedpakr reads directly. read_member_bytes() is the one
# the compatibility issue calls out: it landed in d876ded and older hosts
# import their sloppak module cleanly but don't provide it.
READ_MEMBER_BYTES = CoreHelper(
    module='sloppak',
    attr='read_member_bytes',
    landed=CORE_BASELINE_COMMIT_SHORT,
    fallback=(
        "feedpakr's own path-traversal- and size-capped zip/dir reader "
        '(feedpakr_upgrade._read_member)'
    ),
)
LOAD_MANIFEST = CoreHelper(
    module='sloppak',
    attr='load_manifest',
    landed='pre-baseline',
    fallback=(
        "feedpakr's own bounded YAML manifest reader "
        '(feedpakr_upgrade._load_manifest_fallback)'
    ),
)

EXTERNAL_TOOLS: dict[str, ExternalTool] = {
    'ffmpeg': ExternalTool(
        label='FFmpeg',
        purpose='transcode audio to a browser-safe codec and mix separated stems for autosync',
        binary='ffmpeg',
    ),
    'ffprobe': ExternalTool(
        label='ffprobe',
        purpose='read an audio file\'s duration',
        binary='ffprobe',
    ),
    'fluidsynth': ExternalTool(
        label='FluidSynth',
        purpose='render GP3-5 audio from MIDI (used by gp2midi)',
        binary='fluidsynth',
        module='fluidsynth',
    ),
    'yt-dlp': ExternalTool(
        label='yt-dlp',
        purpose='fetch a YouTube/Vimeo/etc. recording for the sync audio mode',
        module='yt_dlp',
    ),
}

OPTIONAL_SIBLINGS: dict[str, str] = {
    'song_preview': 'Generate Preview handoff',
    'stem_splitter': 'Split Stems handoff',
    'lyrics_sync': 'Sync Lyrics handoff',
    'difficulty_ladder': 'Generate Difficulty Ladder handoff',
    'chordr': 'optional in-import chord naming',
}

FEATURES: dict[str, Feature] = {
    'gp_import': Feature(
        key='gp_import',
        label='Guitar Pro import (GP3-GP8)',
        description=(
            'Parse a Guitar Pro file and build a feedpak. gp2rs/song are the '
            'core conversion path; guitarpro and gp2rs_gpx are the '
            'format-specific readers.'
        ),
        core_modules=('gp2rs', 'song'),
        optional_core_modules=('guitarpro', 'gp2rs_gpx', 'gp2midi', 'gp8_audio_sync', 'gp_autosync'),
        external_tools=('ffmpeg', 'ffprobe', 'fluidsynth', 'yt-dlp'),
        optional_siblings=('song_preview', 'stem_splitter', 'lyrics_sync', 'difficulty_ladder', 'chordr'),
        note=(
            'guitarpro is required for GP3-5 (.gp3/.gp4/.gp5); gp2rs_gpx for '
            'GP6-8 (.gpx/.gp). Audio modes add their own optional core modules '
            '(gp2midi / gp8_audio_sync / gp_autosync) and external tools.'
        ),
    ),
    'existing_pack_reuse': Feature(
        key='existing_pack_reuse',
        label='Existing-pack audio reuse',
        description=(
            "Re-import a GP chart while keeping a .sloppak/.feedpak's own "
            'audio, stems, and cover, re-aligning the new chart via autosync.'
        ),
        core_modules=('gp2rs', 'song'),
        optional_core_modules=('gp_autosync',),
        core_helpers=(READ_MEMBER_BYTES,),
        external_tools=('ffmpeg', 'ffprobe'),
        note=(
            'gp_autosync is needed to realign the reused audio to the fresh '
            'chart; ffmpeg is only needed when the source pack has no full '
            'mixdown and its separated stems must be mixed into a reference.'
        ),
    ),
    'upgrade': Feature(
        key='upgrade',
        label='Upgrade .sloppak to .feedpak',
        description=(
            'Batch-convert existing .sloppak files to .feedpak. Reads members '
            "through the host's sloppak reader when present, else feedpakr's "
            'own standalone reader.'
        ),
        core_helpers=(LOAD_MANIFEST, READ_MEMBER_BYTES),
    ),
    'duplicate_scan': Feature(
        key='duplicate_scan',
        label='Duplicate .feedpak scan and cleanup',
        description=(
            'Hash .feedpak member content to find byte-identical duplicates '
            'and move caller-selected copies to a recoverable trash folder.'
        ),
        core_helpers=(READ_MEMBER_BYTES,),
    ),
    'output_playback': Feature(
        key='output_playback',
        label='Playback of built feedpaks',
        description=(
            "The host's own reader plays back what feedpakr writes. This is "
            'decided by the installed host, not by feedpakr, so it is '
            'documented rather than probed here.'
        ),
        probed=False,
        note=(
            'Packs that use features added to the host reader after the '
            'baseline (e.g. multiple drum arrangements) may require a newer '
            f'host than {CORE_BASELINE_COMMIT_SHORT}; regenerate or reinstall '
            'to pick it up. feedpakr self-validates against the feedpak-spec '
            'schemas regardless of host version.'
        ),
    ),
}

# The pack-read helpers this plugin treats as its core-helper contract.
_SLOPPAK_HELPERS = ('load_manifest', 'read_member_bytes')


# ── Runtime probes ─────────────────────────────────────────────────────────

def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def probe_modules(names) -> dict[str, bool]:
    """Whether each named module is importable in this environment."""
    return {name: _module_available(name) for name in names}


def has_helper(module: str, attr: str) -> bool:
    """Whether ``module.attr`` exists.

    Deliberately a real import + hasattr, not just a module-presence check:
    an older sloppak that imports fine but predates read_member_bytes() is
    exactly the case that used to raise AttributeError instead of falling
    back.
    """
    try:
        mod = importlib.import_module(module)
    except Exception:
        return False
    return hasattr(mod, attr)


def probe_helpers(helpers) -> dict[str, bool]:
    return {h.key: has_helper(h.module, h.attr) for h in helpers}


def sloppak_missing_helpers(module) -> list[str]:
    """Pack-read helpers the given sloppak module lacks (empty if all there).

    ``module`` may be None (sloppak not importable at all).
    """
    if module is None:
        return list(_SLOPPAK_HELPERS)
    return [attr for attr in _SLOPPAK_HELPERS if not hasattr(module, attr)]


def _tool_available(tool: ExternalTool) -> bool:
    if tool.binary and shutil.which(tool.binary):
        return True
    if tool.module and _module_available(tool.module):
        return True
    return False


def probe_external_tools() -> dict[str, bool]:
    return {name: _tool_available(tool) for name, tool in EXTERNAL_TOOLS.items()}


def _baseline_hint() -> str:
    return (
        f'feedpakr is tested against {CORE_BASELINE_REPO} '
        f'core commit {CORE_BASELINE_COMMIT_SHORT}; update the host.'
    )


def evaluate_feature(
    key: str,
    *,
    modules: dict[str, bool] | None = None,
    helpers: dict[str, bool] | None = None,
) -> dict:
    """Report one feature's availability.

    ``modules``/``helpers`` may be pre-resolved maps (as returned by
    ``probe_modules`` / ``probe_helpers``); anything missing from an injected
    map is treated as unavailable, which makes the function easy to test
    without a host.
    """
    feature = FEATURES[key]
    modules = probe_modules(feature.core_modules) if modules is None else modules
    helpers = probe_helpers(feature.core_helpers) if helpers is None else helpers

    missing_modules = [m for m in feature.core_modules if not modules.get(m)]
    missing_helpers = [h for h in feature.core_helpers if not helpers.get(h.key, False)]
    hard_missing = [h for h in missing_helpers if not h.has_fallback]
    degraded = [h for h in missing_helpers if h.has_fallback]

    if not feature.probed:
        status = 'documented'
    elif missing_modules or hard_missing:
        status = 'unavailable'
    elif degraded:
        status = 'degraded'
    else:
        status = 'available'

    if status == 'unavailable':
        parts = []
        if missing_modules:
            parts.append('missing core module(s): ' + ', '.join(missing_modules))
        if hard_missing:
            parts.append('missing core helper(s): ' + ', '.join(h.key for h in hard_missing))
        message = (
            f'{feature.label} is disabled — ' + '; '.join(parts) + '. '
            + feature.note + ' ' + _baseline_hint()
        ).strip()
    elif status == 'degraded':
        detail = '; '.join(f'{h.key} (using {h.fallback})' for h in degraded)
        message = (
            f'{feature.label} is available via a fallback: {detail}. '
            'The host core is older than the tested baseline for this helper.'
        )
    elif status == 'documented':
        message = feature.note
    else:
        message = ''

    return {
        'key': feature.key,
        'label': feature.label,
        'description': feature.description,
        'status': status,
        'ok': status in ('available', 'degraded', 'documented'),
        'missing_core_modules': missing_modules,
        'missing_core_helpers': [h.key for h in hard_missing],
        'degraded_helpers': [
            {'key': h.key, 'landed': h.landed, 'fallback': h.fallback}
            for h in degraded
        ],
        'optional_core_modules': {
            m: modules.get(m, _module_available(m))
            for m in feature.optional_core_modules
        },
        'external_tools': list(feature.external_tools),
        'optional_siblings': list(feature.optional_siblings),
        'message': message,
        'note': feature.note,
    }


def compatibility_report() -> dict:
    """Probe this host and describe every feature's availability."""
    all_modules = sorted({
        m for f in FEATURES.values() for m in (*f.core_modules, *f.optional_core_modules)
    })
    all_helpers = [h for f in FEATURES.values() for h in f.core_helpers]
    modules = probe_modules(all_modules)
    helpers = probe_helpers(all_helpers)
    tools = probe_external_tools()

    features = {
        key: evaluate_feature(key, modules=modules, helpers=helpers)
        for key in FEATURES
    }
    return {
        'core_baseline': {
            'repo': CORE_BASELINE_REPO,
            'commit': CORE_BASELINE_COMMIT,
            'commit_short': CORE_BASELINE_COMMIT_SHORT,
            'version': CORE_BASELINE_VERSION,
            'note': CORE_BASELINE_NOTE,
        },
        'features': features,
        'external_tools': {
            name: {
                'label': tool.label,
                'purpose': tool.purpose,
                'available': tools[name],
            }
            for name, tool in EXTERNAL_TOOLS.items()
        },
        'optional_siblings': dict(OPTIONAL_SIBLINGS),
        'ok': all(f['ok'] for f in features.values()),
    }
