#!/usr/bin/env python3
"""Build a feedpak and report which authored Guitar Pro fields survived.

The report intentionally uses feedpakr's production parser and builder.  It is
both a small CLI and an importable regression helper; no hard-coded song paths
or legacy sloppak assumptions are involved.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any

import yaml


def _bootstrap_core_lib() -> None:
    """Make the CLI usable outside the host without changing plugin imports."""
    candidates = []
    configured = os.environ.get('FEEDBACK_CORE_LIB')
    if configured:
        candidates.append(Path(configured))
    here = Path(__file__).resolve().parent
    candidates.extend((
        here.parent / 'pakr' / 'feedBack' / 'lib',
        here.parent / 'feedBack' / 'lib',
    ))
    for candidate in candidates:
        if (candidate / 'gp2rs.py').is_file():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            return


_bootstrap_core_lib()
import feedpakr_pipeline as pipeline

# The host normally supplies this callback. Standalone reports still need the
# sibling conversion modules (notably drums) from the bootstrapped core lib.
pipeline.configure_sibling_loader(importlib.import_module)


def _all_gp345_beats(song, selected: set[int]):
    for idx, track in enumerate(song.tracks):
        if idx not in selected:
            continue
        for measure in track.measures:
            for voice in measure.voices:
                yield from voice.beats


def _gp345_source(path: str, parsed: dict, selected: set[int]) -> dict[str, Any]:
    song = pipeline.guitarpro.parse(path)
    beats = list(_all_gp345_beats(song, selected))
    notes = [note for beat in beats for note in beat.notes]
    non_vocal = [t for t in parsed['tracks'] if t['index'] in selected and not t.get('is_vocal')]

    def authored_stroke(beat) -> bool:
        effect = getattr(beat, 'effect', None)
        stroke = getattr(getattr(effect, 'stroke', None), 'direction', None)
        pick = getattr(effect, 'pickStroke', None)
        return int(getattr(stroke, 'value', stroke) or 0) != 0 or int(getattr(pick, 'value', pick) or 0) != 0

    return {
        'title': bool((song.title or '').strip()),
        'artist': bool((song.artist or '').strip()),
        'album': bool((song.album or '').strip()),
        'arrangements': len(non_vocal),
        'notes': len(notes),
        'chords': sum(1 for beat in beats if len(beat.notes) > 1),
        'strumming': sum(1 for beat in beats if authored_stroke(beat)),
        'capo': any(getattr(song.tracks[i], 'offset', 0) for i in selected),
        'sections': any(getattr(h, 'marker', None) for h in song.measureHeaders),
        'beats': bool(beats),
        'lyrics': any(
            any((getattr(line, 'lyrics', '') or '').strip()
                for line in getattr(getattr(song.tracks[i], 'lyrics', None), 'lines', []))
            for i in selected
        ),
        'keys': any(getattr(h, 'keySignature', None) is not None for h in song.measureHeaders),
        'drums': sum(1 for t in non_vocal if t.get('is_drums')),
        'notation': 0,
        'tones': bool(pipeline.tones_mod.extract_gp345_tones(song, sorted(selected))),
        'embedded_audio': False,
    }


def _gpif_source(path: str, parsed: dict, selected: set[int]) -> dict[str, Any]:
    root = pipeline.gp2rs_gpx._load_gpif(path)
    tags = [el.tag.rsplit('}', 1)[-1] for el in root.iter()]
    text = lambda name: [el for el in root.iter() if el.tag.rsplit('}', 1)[-1] == name]
    selected_tracks = [t for t in parsed['tracks'] if t['index'] in selected]
    non_vocal = [t for t in selected_tracks if not t.get('is_vocal')]
    beats = text('Beat')
    notes = text('Note')
    chords = sum(
        1 for beat in beats
        if len((beat.findtext('Notes') or '').split()) > 1
    )
    strums = 0
    for beat in beats:
        blob = ' '.join(
            [str(value) for child in beat.iter() for value in child.attrib.values()]
            + [(child.text or '') for child in beat.iter()]
        )
        if 'Brush' in blob or 'Arpeggio' in blob:
            strums += 1
    score = root.find('Score')
    return {
        'title': bool(score is not None and (score.findtext('Title') or '').strip()),
        'artist': bool(score is not None and (score.findtext('Artist') or '').strip()),
        'album': bool(score is not None and (score.findtext('Album') or '').strip()),
        'arrangements': len(non_vocal),
        'notes': len(notes),
        'chords': chords,
        'strumming': strums,
        'capo': any(
            (el.get('name') or '') == 'CapoFret'
            and int(el.findtext('Fret') or 0) > 0
            for el in root.iter()
        ),
        'sections': 'Section' in tags,
        'beats': bool(beats),
        'lyrics': 'Lyrics' in tags or 'Line' in tags,
        'keys': 'KeySignature' in tags,
        'drums': sum(1 for t in non_vocal if t.get('is_drums')),
        'notation': sum(1 for t in non_vocal if t.get('is_piano')),
        'tones': any((el.findtext('Type') or '') in {'Sound', 'Bank'} for el in text('Automation')),
        'embedded_audio': bool(parsed.get('has_embedded_audio')),
    }


def inspect_source(path: str, parsed: dict, selected: set[int]) -> dict[str, Any]:
    if parsed['format'] == 'gpif':
        return _gpif_source(path, parsed, selected)
    return _gp345_source(path, parsed, selected)


def inspect_feedpak(data: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        manifest_name = 'manifest.yaml' if 'manifest.yaml' in names else 'manifest.yml'
        manifest = yaml.safe_load(zf.read(manifest_name))
        arrangements = []
        for entry in manifest.get('arrangements', []):
            rel = entry.get('file')
            if rel and rel in names:
                arrangements.append(json.loads(zf.read(rel)))

        notes = [n for arr in arrangements for n in arr.get('notes', [])]
        chords = [c for arr in arrangements for c in arr.get('chords', [])]
        chord_notes = [n for chord in chords for n in chord.get('notes', [])]
        all_notes = notes + chord_notes
        return {
            'title': bool(manifest.get('title')),
            'artist': bool(manifest.get('artist')),
            'album': bool(manifest.get('album')),
            'arrangements': len(manifest.get('arrangements', [])),
            'notes': len(all_notes),
            'chords': len(chords),
            'strumming': sum(1 for n in all_notes if n.get('pkd', -1) in (0, 1)),
            'capo': any(entry.get('capo', 0) for entry in manifest.get('arrangements', [])),
            'sections': 'song_timeline.json' in names and bool(json.loads(zf.read('song_timeline.json')).get('sections')),
            'beats': 'song_timeline.json' in names and bool(json.loads(zf.read('song_timeline.json')).get('beats')),
            'lyrics': 'lyrics.json' in names,
            'keys': 'keys.json' in names,
            'drums': sum(1 for e in manifest.get('arrangements', []) if e.get('type') == 'drums'),
            'notation': sum(1 for e in manifest.get('arrangements', []) if e.get('notation')),
            'tones': any(arr.get('tones') for arr in arrangements),
            'embedded_audio': any(name.startswith('stems/') for name in names),
        }


def compare(source: dict[str, Any], output: dict[str, Any], *, audio_tested: bool) -> list[dict[str, Any]]:
    rows = []
    for field, expected in source.items():
        actual = output.get(field)
        if field == 'embedded_audio' and expected and not audio_tested:
            status = 'skipped'
        elif not expected:
            status = 'not-present'
        elif isinstance(expected, bool):
            status = 'preserved' if bool(actual) else 'missing'
        elif field in {'notes', 'chords', 'strumming', 'drums', 'notation'}:
            status = 'preserved' if int(actual or 0) > 0 else 'missing'
        else:
            status = 'preserved' if int(actual or 0) >= int(expected) else 'partial' if actual else 'missing'
        rows.append({'field': field, 'source': expected, 'feedpak': actual, 'status': status})
    return rows


def build_report(path: str, *, audio_mode: str = 'none') -> dict[str, Any]:
    parsed = pipeline.parse_gp(path)
    selected = {t['index'] for t in parsed['tracks']}
    names = {t['index']: t.get('auto_name') or t.get('name') or f"Track {t['index'] + 1}" for t in parsed['tracks']}
    notation = {t['index'] for t in parsed['tracks'] if t.get('is_piano')} if parsed['format'] == 'gpif' else None
    result = pipeline.build_feedpak(
        path,
        track_indices=sorted(selected),
        arrangement_names=names,
        notation_track_indices=notation,
        audio_mode=audio_mode,
        report=lambda _stage, _pct: None,
    )
    source = inspect_source(path, parsed, selected)
    output = inspect_feedpak(result['bytes'])
    rows = compare(source, output, audio_tested=audio_mode != 'none')
    return {
        'source_file': str(Path(path).resolve()),
        'format': parsed['format'],
        'audio_mode': audio_mode,
        'coverage': rows,
        'warnings': result['warnings'],
        'missing': [row['field'] for row in rows if row['status'] in {'missing', 'partial'}],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gp_file')
    parser.add_argument('--audio-mode', choices=('none', 'embedded', 'midi'), default='none')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args(argv)
    report = build_report(args.gp_file, audio_mode=args.audio_mode)
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"GP -> feedpak fidelity: {report['source_file']} ({report['format']})")
        for row in report['coverage']:
            print(f"{row['status']:11} {row['field']:16} source={row['source']!s:>6} feedpak={row['feedpak']}")
        if report['warnings']:
            print('\nBuild warnings:')
            for warning in report['warnings']:
                print(f'- {warning}')
    return 1 if report['missing'] else 0


if __name__ == '__main__':
    sys.exit(main())
