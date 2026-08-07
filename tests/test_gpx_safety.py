"""Tests for the GPX decompression-bomb and parse-timeout mitigations.

These tests exercise feedpakr_pipeline._assert_gpx_within_size_limits
and the related _MAX_GPX_XML_BYTES constant.  They use only stdlib
(zipfile, io) and need no feedBack host core lib, so they run everywhere.
"""
from __future__ import annotations

import io
import zipfile

import pytest

import feedpakr_pipeline as pipeline


# ── _assert_gpx_within_size_limits ──────────────────────────────────────────

def _make_gpx(tmp_path, gpif_content: bytes, filename: str = 'song.gpx') -> str:
    """Write a minimal .gpx zip archive containing GPIF.gpif -> gpif_content."""
    p = tmp_path / filename
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('GPIF.gpif', gpif_content)
    return str(p)


def test_assert_gpx_accepts_small_gpif(tmp_path):
    """A small, legitimate GPIF.gpif member must pass without error."""
    content = b'<GPIF><Score><Title>Test</Title></Score></GPIF>'
    path = _make_gpx(tmp_path, content)
    # Must not raise.
    pipeline._assert_gpx_within_size_limits(path)


def test_assert_gpx_rejects_oversized_gpif(tmp_path, monkeypatch):
    """A GPIF.gpif member that expands beyond _MAX_GPX_XML_BYTES must be
    rejected with UnsupportedFormatError — guards the decompression-bomb
    vector where a tiny compressed upload inflates to huge XML."""
    monkeypatch.setattr(pipeline, '_MAX_GPX_XML_BYTES', 1024)
    # 8 KB of highly-compressible content: well over the patched 1 KB cap
    # once decompressed, but small on disk.
    content = b'\x00' * 8 * 1024
    path = _make_gpx(tmp_path, content)
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._assert_gpx_within_size_limits(path)


def test_assert_gpx_rejects_oversized_gpif_error_message(tmp_path, monkeypatch):
    """The rejection message must mention the MB limit."""
    monkeypatch.setattr(pipeline, '_MAX_GPX_XML_BYTES', 1024)
    content = b'\x00' * 8 * 1024
    path = _make_gpx(tmp_path, content)
    with pytest.raises(pipeline.UnsupportedFormatError) as exc_info:
        pipeline._assert_gpx_within_size_limits(path)
    assert 'MB' in str(exc_info.value)


def test_assert_gpx_no_gpif_member_does_not_raise(tmp_path):
    """An archive without GPIF.gpif (corrupt / unknown future variant) must
    not raise — the check passes through and lets the parser fail later."""
    p = tmp_path / 'song.gpx'
    with zipfile.ZipFile(p, 'w') as zf:
        zf.writestr('Something.xml', b'<x/>')
    pipeline._assert_gpx_within_size_limits(str(p))


def test_assert_gpx_bad_zip_raises_unsupported_format_error(tmp_path):
    """A file with a .gpx extension that is not a valid zip must surface as
    UnsupportedFormatError, not a raw zipfile.BadZipFile."""
    p = tmp_path / 'song.gpx'
    p.write_bytes(b'this is not a zip file')
    with pytest.raises(pipeline.UnsupportedFormatError, match='[Gg][Pp][Xx]|zip|archive'):
        pipeline._assert_gpx_within_size_limits(str(p))


def test_check_extension_calls_size_check_for_gpx(tmp_path, monkeypatch):
    """_check_extension must invoke the decompression-bomb guard for .gpx
    files when gp2rs_gpx is available, rejecting an oversized archive before
    any parsing begins."""
    monkeypatch.setattr(pipeline, '_MAX_GPX_XML_BYTES', 1024)
    # Pretend gp2rs_gpx is available so the extension check doesn't short-circuit.
    monkeypatch.setattr(pipeline, 'gp2rs_gpx', object())
    content = b'\x00' * 8 * 1024
    path = _make_gpx(tmp_path, content)
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension(path)


def test_check_extension_calls_size_check_for_gp(tmp_path, monkeypatch):
    """Same guard applies to .gp (GP7/8) files, not only .gpx."""
    monkeypatch.setattr(pipeline, '_MAX_GPX_XML_BYTES', 1024)
    monkeypatch.setattr(pipeline, 'gp2rs_gpx', object())
    content = b'\x00' * 8 * 1024
    path = _make_gpx(tmp_path, content, filename='song.gp')
    with pytest.raises(pipeline.UnsupportedFormatError):
        pipeline._check_extension(path)
