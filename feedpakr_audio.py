"""Audio acquisition for feedpakr: MIDI synthesis, GP8 embedded audio,
autosync against a user-supplied or fetched recording, and YouTube fetch.

Every function here returns (path_or_None, offset, error_or_None) rather
than raising — build_feedpak's audio stage is one of many best-effort
enrichment steps and must degrade to a warning, never abort the import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse


# Allowlist of hostnames accepted by download_youtube_audio.  Only well-known
# video-hosting domains are permitted; this prevents the function from being
# used as a generic SSRF primitive via yt-dlp's hundreds of extractors.
_ALLOWED_VIDEO_HOSTS: frozenset[str] = frozenset({
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'music.youtube.com',
    'youtu.be',
    'vimeo.com',
    'www.vimeo.com',
    'player.vimeo.com',
    'dailymotion.com',
    'www.dailymotion.com',
    'soundcloud.com',
    'www.soundcloud.com',
    'bandcamp.com',
    'www.bandcamp.com',
})


def _is_allowed_video_url(url: str) -> bool:
    """Return True iff *url* targets a known video-hosting domain over http(s).

    Rejects anything non-http(s) (file://, ftp://, …), bare paths, and any
    host not in the explicit allowlist — including numeric IPs and private-range
    addresses that yt-dlp would happily fetch server-side.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    if not host:
        return False
    return host in _ALLOWED_VIDEO_HOSTS

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
        # GP8 FramePadding describes where audio begins relative to bar 1:
        # negative means the recording starts before the chart.  gp2rs uses
        # the opposite convention (seconds added to chart events), so a
        # two-second audio lead-in must move bar 1 forward by two seconds.
        offset = -sync.audio_offset if sync else 0.0
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


def get_audio_duration(audio_path: str) -> float | None:
    """Get the duration of an audio file in seconds using ffprobe.
    Returns None on any error (file not found, ffprobe unavailable, etc.)."""
    if not audio_path or not Path(audio_path).exists():
        return None
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', str(audio_path)
            ],
            capture_output=True, timeout=10, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def transcode_to_ogg(src_path: str, out_path: str, timeout: int = 120):
    """Normalize an arbitrary user-supplied/fetched audio file to OGG so
    every non-embedded, non-synthesized stem meets the feedpak-spec
    baseline codec requirement. The spec allows both OGG and WAV per §5.3.2,
    but browsers reliably support only OGG — so WAV inputs are transcoded to
    OGG for maximum compatibility (spec-valid, browser-compatible). Returns
    (path, error)."""
    src = Path(src_path)
    if src.suffix.lower() == '.ogg':
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
    (path, error).

    Only URLs targeting known video-hosting domains (see _ALLOWED_VIDEO_HOSTS)
    are accepted; all other URLs are rejected to prevent server-side request
    forgery via yt-dlp's generic extractors.
    """
    if not _is_allowed_video_url(url):
        return None, (
            'URL is not from a supported video host. '
            'Accepted hosts: YouTube, Vimeo, Dailymotion, SoundCloud, Bandcamp.'
        )
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
    # Only match completed files; exclude .part/.ytdl (in-progress/aborted downloads).
    candidates = _completed_youtube_downloads(out_dir)
    if candidates:
        return str(candidates[0]), None
    return None, 'Download completed but no audio file was found.'


def _completed_youtube_downloads(out_dir: str | Path) -> list[Path]:
    """Return completed yt-dlp fallback files, excluding partial metadata."""
    completed_suffixes = {'.mp3', '.m4a', '.wav', '.webm', '.mkv', '.flv'}
    return [
        path for path in Path(out_dir).glob('youtube_audio.*')
        if path.suffix.lower() in completed_suffixes
    ]
