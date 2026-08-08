"""Tests for the GPIF decompression-bomb mitigation.

These tests exercise feedpakr_pipeline._assert_gpif_within_size_limits and
the related _MAX_GPIF_XML_BYTES constant. They use only stdlib (zipfile,
struct) and need no feedBack host core lib, so they run everywhere.

Fixtures mirror the real Guitar Pro container layouts (see the host's
gp2rs_gpx._load_gpif): .gp (GP7/8) files are zips holding
Content/score.gpif; .gpx (GP6) files are BCFZ-magic binary containers.
"""
from __future__ import annotations

import struct
import zipfile

import pytest

import feedpakr_pipeline as pipeline


# ── _assert_gpif_within_size_limits ────────────────────────────────────────

def _make_gp_zip(tmp_path, gpif_content: bytes, filename: str = 'song.gp') -> str:
    """Write a minimal .gp (GP7/8) zip archive containing Content/score.gpif."""
    p = tmp_path / filename
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('Content/score.gpif', gpif_content)
    return str(p)


def _make_gpx_bcfz(tmp_path, filename: str = 'song.gpx') -> str:
    """Write a minimal .gpx (GP6) BCFZ-magic container header."""
    p = tmp_path / filename
    p.write_bytes(b'BCFZ' + struct.pack('<I', 8) + b'<GPIF/>')
    return str(p)


def _corrupt_deflate(tmp_path) -> str:
    """Write a .gp zip whose Content/score.gpif deflate stream is garbage —
    a valid zip structure with an unreadable member (raises zlib.error, not
    BadZipFile, on read)."""
    p = tmp_path / 'song.gp'
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('Content/score.gpif', b'\x00' * 4096)
    data = bytearray(p.read_bytes())
    # Local file header: 30-byte fixed part + filename, then compressed data.
    start = 30 + len(b'Content/score.gpif')
    for i in range(start, start + 4):
        data[i] = 0xFF  # invalid deflate block header
    p.write_bytes(bytes(data))
    return str(p)


def test_assert_gpif_accepts_small_gpif(tmp_path):
    """A small, legitimate Content/score.gpif member must pass without error."""
    content = b'<GPIF><Score><Title>Test</Title></Score></GPIF>'
    path = _make_gp_zip(tmp_path, content)
    # Must not raise.
    pipeline._assert_gpif_within_size_limits(path)


def test_assert_gpif_accepts_realistic_gpx(tmp_path):
    """A BCFZ-magic .gpx (GP6) container must pass unchanged — GP6 files are
    not zips, and the host already caps their decompressed size."""
    path = _make_gpx_bcfz(tmp_path)
    pipeline._assert_gpif_within_size_limits(path)


def test_assert_gpif_rejects_oversized_gpif(tmp_path, monkeypatch):
    """A Content/score.gpif member that expands beyond _MAX_GPIF_XML_BYTES
    must be rejected with UnsupportedFormatError — guards the
    decompression-bomb vector where a tiny compressed upload inflates to
    huge XML."""
    monkeypatch.setattr(pipeline, '_MAX_GPIF_XML_BYTES', 1024)
    # 8 KB of highly-compressible content: well over the patched 1 KB cap
    # once decompressed, but small on disk.
    content = b'\x00' * 8 * 1024
    path = _make_gp_zip(tmp_path, content)
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._assert_gpif_within_size_limits(path)


def test_assert_gpif_rejects_oversized_gpif_error_message(tmp_path, monkeypatch):
    """The rejection message must mention the MB limit."""
    monkeypatch.setattr(pipeline, '_MAX_GPIF_XML_BYTES', 1024)
    content = b'\x00' * 8 * 1024
    path = _make_gp_zip(tmp_path, content)
    with pytest.raises(pipeline.UnsupportedFormatError) as exc_info:
        pipeline._assert_gpif_within_size_limits(path)
    assert 'MB' in str(exc_info.value)


def test_assert_gpif_no_score_member_does_not_raise(tmp_path):
    """A zip without Content/score.gpif (corrupt / unknown future variant)
    must not raise — the check passes through and lets the parser fail
    later."""
    p = tmp_path / 'song.gp'
    with zipfile.ZipFile(p, 'w') as zf:
        zf.writestr('Something.xml', b'<x/>')
    pipeline._assert_gpif_within_size_limits(str(p))


def test_assert_gpif_bad_zip_raises_unsupported_format_error(tmp_path):
    """A PK-magic .gp file that is not a valid zip must surface as
    UnsupportedFormatError, not a raw zipfile.BadZipFile."""
    p = tmp_path / 'song.gp'
    p.write_bytes(b'PK\x03\x04this is not a valid zip')
    with pytest.raises(pipeline.UnsupportedFormatError, match='[Gg][Pp][Xx]|zip|archive'):
        pipeline._assert_gpif_within_size_limits(str(p))


def test_assert_gpif_corrupt_deflate_raises_unsupported_format_error(tmp_path):
    """A valid zip whose member carries a corrupt deflate stream raises
    zlib.error (not BadZipFile) — that must surface as UnsupportedFormatError
    too, not a raw zlib.error."""
    path = _corrupt_deflate(tmp_path)
    with pytest.raises(pipeline.UnsupportedFormatError, match='[Gg][Pp][Xx]|zip|archive'):
        pipeline._assert_gpif_within_size_limits(path)


def test_check_extension_rejects_oversized_gp(tmp_path, monkeypatch):
    """_check_extension must invoke the decompression-bomb guard for .gp
    files when gp2rs_gpx is available, rejecting an oversized archive before
    any parsing begins."""
    monkeypatch.setattr(pipeline, '_MAX_GPIF_XML_BYTES', 1024)
    # Pretend gp2rs_gpx is available so the extension check doesn't short-circuit.
    monkeypatch.setattr(pipeline, 'gp2rs_gpx', object())
    content = b'\x00' * 8 * 1024
    path = _make_gp_zip(tmp_path, content)
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension(path)


def test_check_extension_passes_realistic_gpx(tmp_path, monkeypatch):
    """A real BCFZ-magic .gpx (GP6) file must pass _check_extension — the
    guard must not run zipfile on the BCFZ container."""
    monkeypatch.setattr(pipeline, 'gp2rs_gpx', object())
    path = _make_gpx_bcfz(tmp_path)
    pipeline._check_extension(path)
