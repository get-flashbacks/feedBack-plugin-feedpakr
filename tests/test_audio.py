"""Tests for feedpakr_audio.py."""

from pathlib import Path
from types import SimpleNamespace
import pytest

import feedpakr_audio as audio_mod


@pytest.mark.parametrize(('frame_padding_offset', 'chart_offset'), [
    (-2.0, 2.0),
    (0.0, 0.0),
    (1.25, -1.25),
])
def test_embedded_audio_normalizes_gp8_offset_for_chart_times(
        tmp_path, monkeypatch, frame_padding_offset, chart_offset):
    """GP8 positions audio relative to bar 1; gp2rs shifts chart events."""
    extracted = tmp_path / 'embedded.ogg'
    extracted.write_bytes(b'OggS')
    fake_sync = SimpleNamespace(audio_offset=frame_padding_offset)
    fake_module = SimpleNamespace(
        has_embedded_audio=lambda _path: True,
        extract_audio=lambda _path, _out: str(extracted),
        extract_sync=lambda _path: fake_sync,
    )
    monkeypatch.setattr(audio_mod, 'gp8_audio_sync', fake_module)

    path, offset, error = audio_mod.extract_embedded_audio(
        str(tmp_path / 'song.gp'), str(tmp_path))

    assert path == str(extracted)
    assert offset == chart_offset
    assert error is None


def test_transcode_to_ogg_skips_ogg_files(tmp_path):
    """OGG files are already the browser-compatible baseline — no transcode needed."""
    ogg_src = tmp_path / 'input.ogg'
    ogg_src.write_bytes(b'\xff\xfb' + b'OGG_PLACEHOLDER' * 100)
    out_path = tmp_path / 'out.ogg'

    result_path, err = audio_mod.transcode_to_ogg(str(ogg_src), str(out_path))

    assert err is None
    # Result should point back to the input (no new file created)
    assert Path(result_path).samefile(ogg_src)
    assert not out_path.exists()


def test_transcode_to_ogg_converts_wav_to_ogg(tmp_path):
    """WAV files are spec-compliant but browsers don't reliably support them —
    transcode_to_ogg must convert WAV to OGG (browser-safe baseline).

    Regression test for bug where transcode_to_ogg returned WAV as-is, causing
    playback to hang with "waiting/buffering" in browsers that don't support WAV."""
    wav_src = tmp_path / 'input.wav'
    wav_src.write_bytes(b'RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00' +
                        b'\x01\x00\x02\x00D\xac\x00\x00\x10\xb1\x02\x00\x04\x00\x10\x00' +
                        b'data\x00\x00\x00\x00')
    out_path = tmp_path / 'out.ogg'

    result_path, err = audio_mod.transcode_to_ogg(str(wav_src), str(out_path))

    # If ffmpeg is not available, this will fail with a clear error (acceptable
    # for test environment where ffmpeg may not be installed)
    if err and 'ffmpeg not found' in err:
        pytest.skip('ffmpeg not available')

    assert err is None, f'transcode failed: {err}'
    assert result_path == str(out_path)
    assert out_path.exists()
    # Output should be OGG (starts with OGG signature)
    assert out_path.read_bytes()[:4] == b'OggS'


def test_youtube_download_ignores_partial_files(tmp_path):
    """Regression: download_youtube_audio's fallback should NOT match .part/.ytdl files.

    yt-dlp leaves .part/.ytdl files when downloads abort (network flake, throttling,
    outdated yt-dlp). The old code grabbed any youtube_audio.* file, which matched
    these fragments — transcode_to_ogg dutifully converted the 2-3 second fragment
    into a clean-looking short OGG, causing silent import failure."""
    # Simulate aborted download state: .part and .ytdl exist, but not .ogg
    (tmp_path / 'youtube_audio.part').write_bytes(b'partial data')
    (tmp_path / 'youtube_audio.ytdl').write_bytes(b'metadata')

    candidates = audio_mod._completed_youtube_downloads(tmp_path)

    # Should find nothing (both files are filtered out)
    assert len(candidates) == 0


def test_youtube_download_fallback_accepts_valid_formats(tmp_path):
    """Fallback should accept valid audio formats (.mp3, .m4a, .wav, .webm, .mkv, .flv)."""
    (tmp_path / 'youtube_audio.mp3').write_bytes(b'mp3 data')
    (tmp_path / 'youtube_audio.m4a').write_bytes(b'm4a data')

    candidates = audio_mod._completed_youtube_downloads(tmp_path)

    assert len(candidates) == 2
    assert all(p.name in ('youtube_audio.mp3', 'youtube_audio.m4a') for p in candidates)


def test_get_audio_duration_missing_file():
    """get_audio_duration should return None for non-existent files."""
    result = audio_mod.get_audio_duration('/nonexistent/file.mp3')
    assert result is None


def test_get_audio_duration_invalid_file(tmp_path):
    """get_audio_duration should return None for non-audio files."""
    invalid_file = tmp_path / 'not_audio.txt'
    invalid_file.write_text('just text')

    result = audio_mod.get_audio_duration(str(invalid_file))
    # If ffprobe is available, this will return None (invalid format);
    # if not available, this will also return None.
    assert result is None


def test_get_audio_duration_uses_supported_ffprobe_writer_options(tmp_path, monkeypatch):
    audio_file = tmp_path / 'audio.ogg'
    audio_file.write_bytes(b'OggS')
    captured = {}

    class Result:
        returncode = 0
        stdout = '123.5\n'

    def fake_run(command, **kwargs):
        captured['command'] = command
        return Result()

    monkeypatch.setattr(audio_mod.subprocess, 'run', fake_run)
    assert audio_mod.get_audio_duration(str(audio_file)) == 123.5
    writer = captured['command'][captured['command'].index('-of') + 1]
    assert writer == 'default=noprint_wrappers=1:nokey=1'
