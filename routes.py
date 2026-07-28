"""feedpakr plugin — backend routes.

Endpoints (mirrors the musicxml-import plugin's shape — see its routes.py
for the pattern this is deliberately consistent with):

  POST /api/plugins/feedpakr/upload
    Receives a Guitar Pro file as base64. Parses it and returns metadata +
    track list for the UI to display. Saves the raw bytes to a temp file
    for the build step, returning an opaque token (never a filesystem path).

  POST /api/plugins/feedpakr/upload-cover
    Attaches cover art to an existing upload token.

  WS   /ws/plugins/feedpakr/build
    Builds a .feedpak from the uploaded GP file, streaming progress
    messages, then writes it into the DLC folder and indexes it.

Phase 1 scope: GP3/GP4/GP5 only (see feedpakr_pipeline.py). GP6/GP7/GP8,
the sloppak-upgrade batch flow, and post-import handoffs to the
song-preview / stem-splitter plugins land in later phases.
"""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

_get_dlc_dir = None
_extract_meta = None
_meta_db = None
_log = None
_pipeline = None
_pack = None

# GP files are binary (zip-compressed for .gpx/.gp, raw for .gp3/4/5) and
# can run larger than a MusicXML score — 30 MB comfortably covers real-world
# tabs while still bounding the base64 payload.
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_MAX_COVER_BYTES = 8 * 1024 * 1024

# Server-side upload registry: opaque token -> {gp_path, cover_path, ts}.
# The build WS receives only the token, never a filesystem path — see the
# same rationale in musicxml-import/routes.py.
_UPLOAD_TTL_SECONDS = 3600
_uploads: dict[str, dict] = {}

_SUPPORTED_EXTENSIONS = {'.gp3', '.gp4', '.gp5', '.gpx', '.gp'}


def _purge_stale_uploads() -> None:
    now = time.monotonic()
    for token in [
        t for t, entry in _uploads.items()
        if now - entry['ts'] > _UPLOAD_TTL_SECONDS
    ]:
        entry = _uploads.pop(token)
        shutil.rmtree(entry['dir'], ignore_errors=True)


def _decode_upload(data: dict, *, max_bytes: int, allowed_exts: set[str] | None = None):
    filename = data.get('filename', '')
    b64 = data.get('data', '')
    if not filename or not b64:
        return {'error': 'No file data'}

    ext = Path(filename).suffix.lower()
    if allowed_exts is not None and ext not in allowed_exts:
        return {'error': f'Unsupported format ({ext}).'}

    if len(b64) > max_bytes * 4 // 3 + 4:
        return {'error': f'File too large (max {max_bytes // (1024 * 1024)} MB)'}

    try:
        return base64.b64decode(b64, validate=True)
    except Exception:
        return {'error': 'Invalid base64 data'}


def setup(app, context):
    global _get_dlc_dir, _extract_meta, _meta_db, _log, _pipeline, _pack
    _get_dlc_dir = context['get_dlc_dir']
    _extract_meta = context['extract_meta']
    _meta_db = context['meta_db']
    _log = context['log']
    _pipeline = context['load_sibling']('feedpakr_pipeline')
    _pack = context['load_sibling']('feedpakr_pack')

    @app.post('/api/plugins/feedpakr/upload')
    async def upload_gp(data: dict):
        """Receive a GP file as base64, parse it, return a summary + token."""
        gp_bytes = _decode_upload(
            data, max_bytes=_MAX_UPLOAD_BYTES, allowed_exts=_SUPPORTED_EXTENSIONS,
        )
        if isinstance(gp_bytes, dict):
            return gp_bytes
        filename = data.get('filename', '')

        _purge_stale_uploads()

        safe_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename) or 'tab.gp5'
        tmp_dir = Path(tempfile.mkdtemp(prefix='feedpakr_'))
        gp_path = tmp_dir / safe_filename
        gp_path.write_bytes(gp_bytes)

        try:
            parsed = _pipeline.parse_gp(str(gp_path))
        except _pipeline.UnsupportedFormatError as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {'error': str(e)}
        except Exception as e:
            _log.exception('feedpakr: GP parse error')
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {'error': f'Failed to parse: {e}'}

        token = secrets.token_hex(16)
        _uploads[token] = {'dir': tmp_dir, 'gp_path': gp_path, 'cover_path': None, 'ts': time.monotonic()}

        return {
            'upload_id': token,
            'title': parsed['title'],
            'artist': parsed['artist'],
            'album': parsed['album'],
            'tracks': parsed['tracks'],
        }

    @app.post('/api/plugins/feedpakr/upload-cover')
    async def upload_cover(data: dict):
        """Attach cover art to an existing upload token."""
        token = data.get('upload_id', '')
        entry = _uploads.get(token)
        if entry is None:
            return {'error': 'Unknown or expired upload_id'}

        img_bytes = _decode_upload(
            data, max_bytes=_MAX_COVER_BYTES,
            allowed_exts={'.jpg', '.jpeg', '.png', '.webp'},
        )
        if isinstance(img_bytes, dict):
            return img_bytes

        filename = data.get('filename', 'cover.jpg')
        ext = Path(filename).suffix.lower() or '.jpg'
        cover_path = entry['dir'] / f'cover{ext}'
        cover_path.write_bytes(img_bytes)
        entry['cover_path'] = cover_path
        return {'ok': True}

    @app.websocket('/ws/plugins/feedpakr/build')
    async def ws_build(
        websocket: WebSocket,
        upload_id: str,
        tracks: str = '',
        names: str = '',
        title: str = '',
        artist: str = '',
        album: str = '',
        audio: str = '1',
    ):
        """Build a .feedpak from the uploaded GP file, stream progress.

        tracks: comma-separated selected track indices, e.g. "0,2,3"
        names:  comma-separated "idx:Name" pairs for renamed arrangements,
                e.g. "0:Lead,2:Bass" — indices without an entry fall back
                to gp2rs's own auto-naming.
        """
        await websocket.accept()
        want_audio = audio not in ('0', 'false', 'no')

        dlc = _get_dlc_dir()
        if not dlc:
            await websocket.send_json({'error': 'DLC folder not configured'})
            await websocket.close()
            return

        entry = _uploads.pop(upload_id, None)
        if entry is not None and time.monotonic() - entry['ts'] > _UPLOAD_TTL_SECONDS:
            shutil.rmtree(entry['dir'], ignore_errors=True)
            entry = None
        if entry is None or not Path(entry['gp_path']).exists():
            await websocket.send_json({'error': 'File expired — please upload again'})
            await websocket.close()
            return

        try:
            track_indices = [int(x) for x in tracks.split(',') if x.strip() != '']
        except ValueError:
            await websocket.send_json({'error': 'Invalid tracks parameter'})
            await websocket.close()
            return

        arrangement_names: dict[int, str] = {}
        for pair in names.split(','):
            if ':' not in pair:
                continue
            idx_str, name = pair.split(':', 1)
            try:
                arrangement_names[int(idx_str)] = name
            except ValueError:
                continue

        gp_path = str(entry['gp_path'])
        cover_path = str(entry['cover_path']) if entry['cover_path'] else None
        tmp_dir = entry['dir']

        progress_queue: asyncio.Queue = asyncio.Queue()

        def _report(stage: str, pct: int) -> None:
            progress_queue.put_nowait({'stage': stage, 'progress': pct})

        def _do_build():
            try:
                result = _pipeline.build_feedpak(
                    gp_path,
                    track_indices=track_indices,
                    arrangement_names=arrangement_names,
                    title=title, artist=artist, album=album,
                    want_audio=want_audio,
                    cover_path=cover_path,
                    report=_report,
                )

                safe_t = _pack.sanitize_filename_component(result['title'], 60)
                safe_a = _pack.sanitize_filename_component(result['artist'], 40)
                base_name = f'{safe_t}_{safe_a}' if safe_a else safe_t

                out_dir = Path(dlc) / 'feedpakr'
                out_path = _pack.unique_output_path(out_dir, base_name)
                out_path.write_bytes(result['bytes'])

                try:
                    rel_name = str(Path('feedpakr') / out_path.name)
                    meta = _extract_meta(out_path)
                    stat = out_path.stat()
                    _meta_db.put(rel_name, stat.st_mtime, stat.st_size, meta)
                except Exception:
                    _log.warning('feedpakr: metadata indexing failed for %r', out_path.name, exc_info=True)

                progress_queue.put_nowait({
                    'done': True,
                    'progress': 100,
                    'stage': 'Complete!',
                    'filename': out_path.name,
                    'arrangement_count': result['arrangement_count'],
                    'duration': result['duration'],
                    'warnings': result['warnings'],
                    'valid': not result['validation'],
                })
            except _pipeline.UnsupportedFormatError as e:
                progress_queue.put_nowait({'error': str(e)})
            except Exception as e:
                _log.exception('feedpakr: build error')
                progress_queue.put_nowait({'error': str(e)})
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        loop = asyncio.get_running_loop()
        build_task = loop.run_in_executor(None, _do_build)

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                    await websocket.send_json(msg)
                    if msg.get('done') or msg.get('error'):
                        break
                except asyncio.TimeoutError:
                    if build_task.done():
                        break
        except WebSocketDisconnect:
            pass

        await websocket.close()

    _log.info('feedpakr: routes registered')
