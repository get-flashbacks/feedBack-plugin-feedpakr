"""Audio acquisition for feedpakr: MIDI synthesis, GP8 embedded audio,
autosync against a user-supplied or fetched recording, and YouTube fetch.

Every function here returns (path_or_None, offset, error_or_None) rather
than raising — build_feedpak's audio stage is one of many best-effort
enrichment steps and must degrade to a warning, never abort the import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

try:
    import gp2midi
except ImportError:  # pragma: no cover
    gp2midi = None

try:
    import gp8_audio_sync
except ImportError:  # pragma: no cover
    gp8_audio_sync = None

try:
    import gp_autosync
except ImportError:  # pragma: no cover
    gp_autosync = None


def synth_midi_audio(gp_path: str, track_indices: list[int], out_base: str):
    """GP3/4/5 only — FluidSynth-rendered audio. Returns (path, 0.0, error)."""
    if gp2midi is None:
        return None, 0.0, 'gp2midi not available on this host.'
    try:
        path = gp2midi.gp_to_audio(gp_path, out_base, track_indices)
        return path, 0.0, None
    except Exception as e:
        return None, 0.0, str(e)


def extract_embedded_audio(gp_path: str, out_dir: str):
    """GP8 only — the backing track baked into the .gp file. Already OGG
    (gp8_audio_sync transcodes non-OGG assets itself). Returns
    (path, offset, error)."""
    if gp8_audio_sync is None:
        return None, 0.0, 'gp8_audio_sync not available on this host.'
    try:
        if not gp8_audio_sync.has_embedded_audio(gp_path):
            return None, 0.0, 'File has no embedded audio.'
        path = gp8_audio_sync.extract_audio(gp_path, out_dir)
        if not path:
            return None, 0.0, 'Failed to extract embedded audio.'
        sync = gp8_audio_sync.extract_sync(gp_path)
        offset = sync.audio_offset if sync else 0.0
        return path, offset, None
    except Exception as e:
        return None, 0.0, str(e)


def autosync_available() -> bool:
    return gp_autosync is not None and gp_autosync.is_available()


def autosync_audio(gp_path: str, audio_path: str, progress_cb=None):
    """Align a user-supplied/fetched recording to the chart via chroma DTW.
    Returns (offset, sync_points, error). sync_points is a JSON-friendly
    list for the UI to show what was found."""
    if gp_autosync is None:
        return 0.0, [], 'gp_autosync not available on this host.'
    if not gp_autosync.is_available():
        return 0.0, [], 'Autosync needs librosa/numpy, not installed on this host.'
    try:
        data = gp_autosync.auto_sync(gp_path, audio_path, progress_cb=progress_cb)
        points = [
            {'bar': sp.bar, 'time_secs': sp.time_secs, 'modified_bpm': sp.modified_tempo}
            for sp in data.sync_points
        ]
        return data.audio_offset, points, None
    except Exception as e:
        return 0.0, [], str(e)


def transcode_to_ogg(src_path: str, out_path: str, timeout: int = 120):
    """Normalize an arbitrary user-supplied/fetched audio file to OGG so
    every non-embedded, non-synthesized stem meets the feedpak-spec
    baseline codec requirement (OGG/WAV MUST). Returns (path, error)."""
    src = Path(src_path)
    if src.suffix.lower() in ('.ogg', '.wav'):
        return str(src), None
    try:
        result = subprocess.run(
            ['ffmpeg', '-y', '-i', str(src), '-q:a', '6', out_path],
            capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, 'ffmpeg not found — cannot transcode to a baseline codec.'
    except subprocess.TimeoutExpired:
        return None, 'ffmpeg transcode timed out.'
    if result.returncode != 0 or not Path(out_path).exists():
        return None, f'ffmpeg transcode failed: {result.stderr[-300:].decode(errors="replace")}'
    return out_path, None


def download_youtube_audio(url: str, out_dir: str, timeout: int = 300):
    """Fetch a YouTube video's audio track as OGG via yt_dlp. Returns
    (path, error)."""
    try:
        import yt_dlp
    except ImportError:
        return None, 'yt-dlp not available on this host.'

    out_template = str(Path(out_dir) / 'youtube_audio.%(ext)s')
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'vorbis',
            'preferredquality': '5',
        }],
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': timeout,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return None, str(e)

    ogg_path = Path(out_dir) / 'youtube_audio.ogg'
    if ogg_path.exists():
        return str(ogg_path), None
    # yt_dlp/ffmpeg unavailable postprocessor fallback — whatever extension
    # landed, still usable (transcode_to_ogg normalizes it downstream).
    candidates = list(Path(out_dir).glob('youtube_audio.*'))
    if candidates:
        return str(candidates[0]), None
    return None, 'Download completed but no audio file was found.'
