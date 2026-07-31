"""Tests for feedpakr_audio.py."""

from pathlib import Path
import pytest

import feedpakr_audio as audio_mod


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
