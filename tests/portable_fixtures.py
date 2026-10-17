"""Repository-authored fixtures generated at test time.

These replace the developer-local sample library
(``c:\\Users\\PC\\Downloads\\Alll``) for the conversion regressions that must
run on *every* CI run. They contain no song data — just a few authored
measures — so they are freely redistributable and tiny, and they are built
deterministically rather than committed as binary blobs.

Two formats are produced, which lets the portable suite cover the GPIF
(GP7/8) and GP3-5 import paths *and* the ways they differ::

    write_gpif_fixture(path)    # a zip + ``Content/score.gpif`` XML (GP7/8)
    write_gp345_fixture(path)   # a real GP4 binary via pyguitarpro

Both fixtures express the same musical intent (three named sections, a
couple of multi-note chords, drums) so the tests can assert the same
high-level behavior across both parsers.

``pyguitarpro`` is imported lazily inside :func:`write_gp345_fixture` so
this module stays importable (and collectible) in a plugin-only checkout
without the host lib.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# ── GPIF (GP7/8) fixture ──────────────────────────────────────────────────
#
# Sections come from ``<MasterBar><Section><Text>``; the host lowercases and
# strips spaces from the name (``gp2rs_gpx``). The low string is dropped to
# D (MIDI 38) so the test can prove the absolute ``<Pitches>`` tuning is
# converted to a relative offset — a GPIF-only behavior GP3-5 files can't
# express the same way.
GPIF_LOW_MIDI = 38  # dropped low E -> D
GPIF_PITCHES = f'{GPIF_LOW_MIDI} 45 50 55 59 64'

GPIF_XML = f'''<?xml version="1.0" encoding="utf-8"?>
<GPIF>
  <Score><Title>Portable GPIF Fixture</Title><Artist>feedpakr CI</Artist></Score>
  <Tracks>
    <Track id="0">
      <Name>Lead Guitar</Name>
      <Property name="Tuning"><Pitches>{GPIF_PITCHES}</Pitches></Property>
    </Track>
  </Tracks>
  <MasterBars>
    <MasterBar><Time>4/4</Time><Bars>0</Bars><Section><Text>Intro</Text></Section></MasterBar>
    <MasterBar><Time>4/4</Time><Bars>1</Bars><Section><Text>Verse</Text></Section></MasterBar>
    <MasterBar><Time>4/4</Time><Bars>2</Bars><Section><Text>Chorus</Text></Section></MasterBar>
  </MasterBars>
  <Bars>
    <Bar id="0"><Voices>0</Voices></Bar>
    <Bar id="1"><Voices>1</Voices></Bar>
    <Bar id="2"><Voices>2</Voices></Bar>
  </Bars>
  <Voices>
    <Voice id="0"><Beats>0</Beats></Voice>
    <Voice id="1"><Beats>1</Beats></Voice>
    <Voice id="2"><Beats>2</Beats></Voice>
  </Voices>
  <Beats>
    <Beat id="0"><Rhythm ref="r0"/><Notes>0 1</Notes></Beat>
    <Beat id="1"><Rhythm ref="r0"/><Notes>2</Notes></Beat>
    <Beat id="2"><Rhythm ref="r0"/><Notes>3 4</Notes></Beat>
  </Beats>
  <Notes>
    <Note id="0"><Property name="String"><String>0</String></Property><Property name="Fret"><Fret>3</Fret></Property></Note>
    <Note id="1"><Property name="String"><String>1</String></Property><Property name="Fret"><Fret>2</Fret></Property></Note>
    <Note id="2"><Property name="String"><String>5</String></Property><Property name="Fret"><Fret>0</Fret></Property></Note>
    <Note id="3"><Property name="String"><String>0</String></Property><Property name="Fret"><Fret>5</Fret></Property></Note>
    <Note id="4"><Property name="String"><String>1</String></Property><Property name="Fret"><Fret>4</Fret></Property></Note>
  </Notes>
  <Rhythms><Rhythm id="r0"><NoteValue>Quarter</NoteValue></Rhythm></Rhythms>
</GPIF>
'''

# Two simultaneous-note beats are read as chords; the fret shapes are what
# the chord/template consistency test asserts on.
GPIF_EXPECTED_CHORD_SHAPES = (
    ((0, 3), (1, 2)),  # Intro beat
    ((0, 5), (1, 4)),  # Chorus beat
)

# ── GP3-5 (GP4) fixture ───────────────────────────────────────────────────
#
# pyguitarpro can write GP3/GP4 reliably (GP5 writing desyncs on read-back),
# so GP4 is the version under test. String numbering is 1-based throughout
# pyguitarpro — a 0 here writes a malformed beat (feedpakr#81 investigation).
GP345_SECTIONS = ('Intro', 'Verse', 'Chorus')
GP345_LEAD_CHORDS = (
    ((1, 3), (2, 2)),
    ((1, 5), (2, 4)),
    ((1, 3), (2, 2)),  # repeats the first shape on purpose → template reuse
)
#: The fret shapes as they appear in the arrangement JSON. pyguitarpro numbers
#: strings 1-based from the *high* E (string 1 = value 64), while feedpakr's
#: ``s`` index is 0-based from the low string, so ``s = 6 - string``.
GP345_EXPECTED_CHORD_SHAPES = tuple(
    tuple(sorted((6 - string, fret) for string, fret in shape))
    for shape in GP345_LEAD_CHORDS
)
GP345_DRUM_HITS = (36, 38, 36)  # kick, snare, kick (see lib/drums.midi_to_piece)
GP345_DRUM_PIECE_NAMES = {'kick': 'Kick', 'snare': 'Snare'}


def write_gpif_fixture(path: Path) -> Path:
    """Write the GP7/8-style GPIF fixture (a zip with ``Content/score.gpif``)."""
    path = Path(path)
    # Fixed member timestamp so the bytes are reproducible across runs.
    info = zipfile.ZipInfo('Content/score.gpif', date_time=(1980, 1, 1, 0, 0, 0))
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(info, GPIF_XML)
    return path


def write_gp345_fixture(path: Path) -> Path:
    """Write the GP3-5 fixture as a real GP4 binary via pyguitarpro."""
    import guitarpro  # host lib; imported lazily — see module docstring

    song = guitarpro.Song()
    song.versionTuple = (4, 0, 6)
    song.title = 'Portable GP4 Fixture'
    song.artist = 'feedpakr CI'

    # Song() already starts with one measure; add one per remaining section so
    # the fixture has exactly one (marked) measure per section — no trailing
    # unnamed measure.
    for _ in range(len(GP345_SECTIONS) - 1):
        song.newMeasure()
    for header, title in zip(song.measureHeaders, GP345_SECTIONS):
        header.marker = guitarpro.Marker(title)

    lead = song.tracks[0]
    lead.name = 'Lead Guitar'
    for measure, shape in zip(lead.measures, GP345_LEAD_CHORDS):
        voice = measure.voices[0]
        beat = guitarpro.Beat(voice)
        beat.status = guitarpro.BeatStatus.normal
        voice.beats.append(beat)
        for string, fret in shape:
            beat.notes.append(guitarpro.Note(
                beat, value=fret, string=string, type=guitarpro.NoteType.normal,
            ))

    drums = guitarpro.Track(song)
    drums.name = 'Drums'
    drums.isPercussionTrack = True
    # On a percussion track the host maps MIDI note = string tuning + fret,
    # so zeroing the tunings lets each note's value be its GM drum number.
    drums.strings = [guitarpro.GuitarString(number, 0) for number in range(1, 7)]
    for header in song.measureHeaders:
        drums.measures.append(guitarpro.Measure(drums, header))
    for measure, midi in zip(drums.measures, GP345_DRUM_HITS):
        voice = measure.voices[0]
        beat = guitarpro.Beat(voice)
        beat.status = guitarpro.BeatStatus.normal
        voice.beats.append(beat)
        beat.notes.append(guitarpro.Note(
            beat, value=midi, string=1, type=guitarpro.NoteType.normal,
        ))
    song.tracks.append(drums)

    guitarpro.write(song, str(path), version=(4, 0, 6))
    return Path(path)
