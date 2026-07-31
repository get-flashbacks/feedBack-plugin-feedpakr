"""Self-validation against vendored feedpak-spec JSON Schemas.

feedpakr never lets a schema failure abort an import (see the plugin's
design notes) — this module only ever returns a report; callers decide
what to do with it (drop the offending side file, warn, etc).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import yaml

try:
    import jsonschema
except ImportError:  # pragma: no cover - exercised only if the plugin's
    jsonschema = None  # requirements.txt entry wasn't installed yet

_SCHEMA_DIR = Path(__file__).parent / 'assets' / 'schemas'
_SCHEMA_CACHE: dict[str, dict] = {}


def _load_schema(name: str) -> dict:
    if name not in _SCHEMA_CACHE:
        path = _SCHEMA_DIR / name
        _SCHEMA_CACHE[name] = json.loads(path.read_text(encoding='utf-8'))
    return _SCHEMA_CACHE[name]


def available() -> bool:
    return jsonschema is not None


def _validate_against(schema_name: str, payload: dict) -> list[str]:
    if jsonschema is None:
        return [f'jsonschema not installed — skipped validation against {schema_name}']
    schema = _load_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f'{".".join(str(p) for p in e.path) or "<root>"}: {e.message}'
        for e in sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    ]


def validate_manifest(manifest: dict) -> list[str]:
    return _validate_against('manifest.schema.json', manifest)


def validate_arrangement(payload: dict) -> list[str]:
    return _validate_against('arrangement.schema.json', payload)


def validate_song_timeline(payload: dict) -> list[str]:
    return _validate_against('song-timeline.schema.json', payload)


def validate_keys(payload: dict) -> list[str]:
    return _validate_against('keys.schema.json', payload)


def validate_vocal_pitch(payload: dict) -> list[str]:
    return _validate_against('vocal-pitch.schema.json', payload)


def validate_drum_tab(payload: dict) -> list[str]:
    return _validate_against('drum-tab.schema.json', payload)


def validate_notation(payload: dict) -> list[str]:
    return _validate_against('notation.schema.json', payload)


def validate_pack(
    *,
    manifest: dict,
    arrangement_files: dict[str, dict],
    song_timeline: dict | None = None,
    keys: dict | None = None,
    vocal_pitch: dict | None = None,
    drum_tab_files: dict[str, dict] | None = None,
    notation_files: dict[str, dict] | None = None,
) -> dict[str, list[str]]:
    """Validate every piece of an in-memory pack. Returns {part: [errors]},
    only including parts that had at least one error."""
    report: dict[str, list[str]] = {}

    errs = validate_manifest(manifest)
    if errs:
        report['manifest.yaml'] = errs

    for filename, payload in arrangement_files.items():
        errs = validate_arrangement(payload)
        if errs:
            report[f'arrangements/{filename}'] = errs

    if song_timeline is not None:
        errs = validate_song_timeline(song_timeline)
        if errs:
            report['song_timeline.json'] = errs

    if keys is not None:
        errs = validate_keys(keys)
        if errs:
            report['keys.json'] = errs

    if vocal_pitch is not None:
        errs = validate_vocal_pitch(vocal_pitch)
        if errs:
            report['vocal_pitch.json'] = errs

    for filename, payload in (drum_tab_files or {}).items():
        errs = validate_drum_tab(payload)
        if errs:
            report[filename] = errs

    for filename, payload in (notation_files or {}).items():
        errs = validate_notation(payload)
        if errs:
            report[filename] = errs

    return report


def validate_feedpak_file(path: str | Path) -> dict[str, list[str]]:
    """Validate the manifest and referenced JSON sidecars in a feedpak ZIP."""
    source = Path(path)
    report: dict[str, list[str]] = {}
    with zipfile.ZipFile(source) as zf:
        manifest_name = next(
            (name for name in ('manifest.yaml', 'manifest.yml') if name in zf.namelist()),
            None,
        )
        if manifest_name is None:
            return {'manifest.yaml': ['<root>: manifest file is missing']}
        try:
            manifest = yaml.safe_load(zf.read(manifest_name).decode('utf-8'))
        except Exception as exc:
            return {manifest_name: [f'<root>: could not parse YAML: {exc}']}
        if not isinstance(manifest, dict):
            return {manifest_name: ['<root>: manifest must be an object']}

        def read_json(rel: str) -> dict | None:
            if not rel:
                return None
            try:
                payload = json.loads(zf.read(rel))
            except KeyError:
                report.setdefault(rel, []).append('<root>: referenced file is missing')
                return None
            except Exception as exc:
                report.setdefault(rel, []).append(f'<root>: could not parse JSON: {exc}')
                return None
            if not isinstance(payload, dict):
                report.setdefault(rel, []).append('<root>: payload must be an object')
                return None
            return payload

        arrangements: dict[str, dict] = {}
        drum_tabs: dict[str, dict] = {}
        notations: dict[str, dict] = {}
        for entry in manifest.get('arrangements', []) or []:
            if not isinstance(entry, dict):
                continue
            rel = entry.get('file')
            payload = read_json(rel) if isinstance(rel, str) else None
            if payload is not None:
                arrangements[rel.removeprefix('arrangements/')] = payload
            for key, dest in (('drum_tab', drum_tabs), ('notation', notations)):
                side_rel = entry.get(key)
                side_payload = read_json(side_rel) if isinstance(side_rel, str) else None
                if side_payload is not None:
                    dest[side_rel] = side_payload

        def referenced(name: str) -> dict | None:
            rel = manifest.get(name)
            return read_json(rel) if isinstance(rel, str) else None

        schema_report = validate_pack(
            manifest=manifest,
            arrangement_files=arrangements,
            song_timeline=referenced('song_timeline'),
            keys=referenced('keys'),
            vocal_pitch=referenced('vocal_pitch'),
            drum_tab_files=drum_tabs,
            notation_files=notations,
        )
        for part, errors in schema_report.items():
            report.setdefault(part, []).extend(errors)
    return report
