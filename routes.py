"""feedpakr plugin — backend routes.

Endpoints (mirrors the musicxml-import plugin's shape — see its routes.py
for the pattern this is deliberately consistent with):

  POST /api/plugins/feedpakr/upload
    Receives a Guitar Pro file as base64. Parses it and returns metadata +
    track list for the UI to display. Saves the raw bytes to a temp file
    for the build step, returning an opaque token (never a filesystem path).

  POST /api/plugins/feedpakr/upload-cover
    Attaches cover art to an existing upload token.

  GET  /api/plugins/feedpakr/cover-search
    Album-centric cover-art candidates from MusicBrainz release-groups
    (studio albums first), for the art picker. Same mechanism as the
    editor plugin's cover search.

  GET  /api/plugins/feedpakr/caa-cover/{cover_id}
    Serves a Cover Art Archive front cover, cached, same-origin (dodges
    browser CORS + CSP for an external host).

  POST /api/plugins/feedpakr/use-caa-cover
    Picks a CAA cover as the upload token's cover art (fetch + cache,
    same effect as upload-cover but sourced from the search results).

  POST /api/plugins/feedpakr/upload-audio
    Attaches a user-supplied audio recording to an existing upload token,
    for the "sync" audio mode (aligned to the chart via autosync at build
    time).

  POST /api/plugins/feedpakr/youtube-audio
    Fetches a YouTube video's audio track and attaches it the same way
    upload-audio does.

  POST /api/plugins/feedpakr/autosync-preview
    Runs autosync against the token's attached audio and returns the
    offset + sync points, without building anything — lets the UI show
    what alignment was found before committing to a build.

  GET  /api/plugins/feedpakr/handoffs
    Reports whether the sibling song-preview / stem-splitter handoff
    endpoints are currently registered on the host app.

  POST /api/plugins/feedpakr/upload-existing-pack
    Attaches an existing .sloppak/.feedpak's audio to an upload token, for
    the 'existing_pack' audio mode — re-imports the GP chart while keeping
    that pack's original audio/stems/cover, re-aligned via autosync.

  WS   /ws/plugins/feedpakr/build
    Builds a .feedpak from the uploaded GP file, streaming progress
    messages, then writes it into the DLC folder and indexes it.

  POST /api/plugins/feedpakr/validate
    Validates an existing DLC-relative .feedpak against vendored schemas.

  GET  /api/plugins/feedpakr/lyrics-text
    Reconstructs plain-text lyrics from an existing DLC-relative feedpak's
    own lyrics.json (spec §7.1 entries -> readable text), for the
    lyrics_sync handoff below — that plugin's /align endpoint wants plain
    text, not pre-synced entries. Reads back only what this plugin already
    wrote; never generates or re-times lyrics itself.

  GET  /api/plugins/feedpakr/sloppaks
    Lists every .sloppak under the DLC folder, for the Upgrade Library tab.

  WS   /ws/plugins/feedpakr/upgrade
    Batch-converts selected .sloppak files to .feedpak, streaming
    per-file progress. Originals are never modified or deleted.

Post-import handoffs to the song-preview / stem-splitter / lyrics_sync
plugins (when installed) are invoked client-side in screen.js. The backend
also exposes a read-only availability endpoint (`/handoffs`, above) so
callers can ask the host which sibling handoff routes are currently
registered, plus (for lyrics_sync only) `/lyrics-text` to read back this
plugin's own already-written lyrics.json as plain text — there is nothing
else for this module to proxy since the sibling plugins expose their own
same-origin REST endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

_get_dlc_dir = None
_extract_meta = None
_meta_db = None
_log = None
_pipeline = None
_pack = None
_audio = None
_upgrade = None
_validate = None
_lyrics = None

# GP files are binary (zip-compressed for .gpx/.gp, raw for .gp3/4/5) and
# can run larger than a MusicXML score — 30 MB comfortably covers real-world
# tabs while still bounding the base64 payload.
_MAX_UPLOAD_BYTES = 30 * 1024 * 1024
_MAX_COVER_BYTES = 8 * 1024 * 1024
_MAX_AUDIO_BYTES = 60 * 1024 * 1024
# Existing packs can carry several separated stems (guitar/bass/drums/
# vocals/piano/other), each already-compressed audio — comfortably larger
# than a single recording upload.
_MAX_EXISTING_PACK_BYTES = 250 * 1024 * 1024

# Maximum seconds to wait for GP file parsing (parse_gp runs in an executor
# thread). wait_for bounds the response time and keeps the event loop free,
# but Python threads can't be killed — a hung parse still occupies its worker
# slot until it returns.
_PARSE_TIMEOUT_SECS = 60

# Extra seconds a timed-out upload's temp dir is kept collectable before the
# stale-upload purger may remove it — gives the executor thread that blew the
# parse timeout a window to finish and drop its open handles.
_PARSE_TIMEOUT_GRACE_SECS = 5

# Server-side upload registry: opaque token -> {gp_path, cover_path, audio_path, ts}.
# The build WS receives only the token, never a filesystem path — see the
# same rationale in musicxml-import/routes.py.
_UPLOAD_TTL_SECONDS = 3600
_uploads: dict[str, dict] = {}

_SUPPORTED_EXTENSIONS = {'.gp3', '.gp4', '.gp5', '.gpx', '.gp'}

_HANDOFF_ROUTES = {
    'preview': ('POST', '/api/plugins/song_preview/backfill'),
    'split': ('POST', '/api/plugins/stem_splitter/split'),
}


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


def _route_registered(app, method: str, path: str) -> bool:
    method = method.upper()
    for route in getattr(app, 'routes', []) or []:
        if getattr(route, 'path', None) != path:
            continue
        methods = getattr(route, 'methods', None)
        if methods is not None and method in methods:
            return True
    return False


def setup(app, context):
    global _get_dlc_dir, _extract_meta, _meta_db, _log, _pipeline, _pack, _audio, _upgrade, _validate, _lyrics
    _get_dlc_dir = context['get_dlc_dir']
    _extract_meta = context['extract_meta']
    _meta_db = context['meta_db']
    _log = context['log']
    _pipeline = context['load_sibling']('feedpakr_pipeline')
    _pipeline.configure_sibling_loader(context['load_sibling'])
    _pack = context['load_sibling']('feedpakr_pack')
    _audio = context['load_sibling']('feedpakr_audio')
    _upgrade = context['load_sibling']('feedpakr_upgrade')
    _validate = context['load_sibling']('feedpakr_validate')
    _lyrics = context['load_sibling']('feedpakr_lyrics')

    @app.get('/api/plugins/feedpakr/handoffs')
    async def handoffs():
        """Report which sibling plugin handoff endpoints are registered."""
        return {
            'handoffs': {
                key: _route_registered(app, method, path)
                for key, (method, path) in _HANDOFF_ROUTES.items()
            }
        }

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
            loop = asyncio.get_running_loop()
            parsed = await asyncio.wait_for(
                loop.run_in_executor(None, _pipeline.parse_gp, str(gp_path)),
                timeout=_PARSE_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            # wait_for cancels the asyncio future, not the executor thread —
            # parse_gp may still be running and holding open file handles
            # inside tmp_dir, so don't rmtree synchronously while it's live
            # (races on Windows; undefined behaviour on all platforms).
            # Register the dir as an upload entry whose TTL already accounts
            # for the parse window (plus a grace period) so _purge_stale_uploads()
            # won't collect it until the thread has had a chance to finish.
            _uploads[secrets.token_hex(16)] = {
                'dir': tmp_dir, 'gp_path': gp_path,
                'cover_path': None, 'audio_path': None,
                'ts': time.monotonic() - _UPLOAD_TTL_SECONDS
                      + _PARSE_TIMEOUT_SECS + _PARSE_TIMEOUT_GRACE_SECS,
            }
            return {'error': 'GP file parsing timed out — file may be too complex or malformed'}
        except _pipeline.UnsupportedFormatError as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {'error': str(e)}
        except Exception as e:
            _log.exception('feedpakr: GP parse error')
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {'error': f'Failed to parse: {e}'}

        token = secrets.token_hex(16)
        _uploads[token] = {
            'dir': tmp_dir, 'gp_path': gp_path,
            'cover_path': None, 'audio_path': None,
            'ts': time.monotonic(),
        }

        return {
            'upload_id': token,
            'title': parsed['title'],
            'artist': parsed['artist'],
            'album': parsed['album'],
            'tracks': parsed['tracks'],
            'format': parsed['format'],
            'has_embedded_audio': parsed['has_embedded_audio'],
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

    # ── Cover Art Archive — album-art picker from MusicBrainz release-groups ──
    # Ported from feedBack-plugin-editor's identical mechanism (routes.py's
    # _mb_release_group_covers / caa-cover / use-caa-cover). Release-groups
    # (not recording releases) carry reliable Album vs Live/Compilation
    # typing, so searching by artist + album/title and preferring the studio
    # album surfaces the canonical cover first. Cached per upload token's
    # temp dir (cleaned up by the existing upload TTL) rather than a
    # persistent STORAGE_DIR — feedpakr has no equivalent shared cache dir.
    _CAA_ID_RE = re.compile(r'^[0-9a-fA-F-]{1,64}$')
    _CAA_MAX_BYTES = 10 * 1024 * 1024
    _CAA_UA = 'feedBack-feedpakr/1.0 ( https://github.com/got-feedBack/feedBack )'
    _CAA_SECONDARY_SKIP = {'live', 'compilation', 'remix', 'dj-mix',
                            'mixtape/street', 'demo', 'interview', 'audiobook',
                            'spokenword'}

    def _caa_fetch_front(cover_id: str, size: int = 500, kind: str = 'release'):
        """Front cover (size px) for a MusicBrainz `kind` ('release' or
        'release-group') from coverartarchive.org, or None on any error /
        missing art. `cover_id` is regex-validated by callers before the URL."""
        import urllib.request
        url = f'https://coverartarchive.org/{kind}/{cover_id}/front-{size}'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': _CAA_UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if getattr(resp, 'status', 200) != 200:
                    return None
                data = resp.read(_CAA_MAX_BYTES + 1)
        except Exception:
            return None
        if not data or len(data) > _CAA_MAX_BYTES or len(data) < 100:
            return None
        return data

    async def _caa_cached(entry: dict, cover_id: str, kind: str = 'release'):
        """Cached cover file (fetch + cache on first use) for this upload
        token, or None when there's no art."""
        tag = 'rg_' if kind == 'release-group' else ''
        dest = entry['dir'] / f'caa_{tag}{cover_id}.jpg'
        if dest.exists() and dest.stat().st_size > 100:
            return dest
        data = await asyncio.get_running_loop().run_in_executor(
            None, _caa_fetch_front, cover_id, 500, kind)
        if data is None:
            return None
        dest.write_bytes(data)
        return dest

    def _mb_release_group_covers(artist: str, query: str) -> list:
        """Release-group search -> [{id, title, year, studio}], studio
        albums first."""
        import urllib.request
        import urllib.parse

        def _phrase(s):
            return s.replace('\\', '\\\\').replace('"', '\\"')

        parts = []
        if query:
            parts.append('releasegroup:"%s"' % _phrase(query))
        if artist:
            parts.append('artist:"%s"' % _phrase(artist))
        if not parts:
            return []
        url = ('https://musicbrainz.org/ws/2/release-group?'
               + urllib.parse.urlencode(
                   {'query': ' AND '.join(parts), 'fmt': 'json', 'limit': 15}))
        try:
            req = urllib.request.Request(url, headers={'User-Agent': _CAA_UA})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode('utf-8', 'replace'))
        except Exception:
            return []
        out = []
        for rg in (body.get('release-groups') or []):
            if not isinstance(rg, dict) or not rg.get('id'):
                continue
            secs = {str(s).lower() for s in (rg.get('secondary-types') or [])}
            studio = (str(rg.get('primary-type', '')).lower() == 'album'
                      and not (secs & _CAA_SECONDARY_SKIP))
            out.append({
                'id': str(rg['id']),
                'title': str(rg.get('title', '') or ''),
                'year': str(rg.get('first-release-date', '') or '')[:4],
                'studio': studio,
            })
        out.sort(key=lambda g: (0 if g['studio'] else 1, g['year'] or '9999'))
        return out

    @app.get('/api/plugins/feedpakr/cover-search')
    async def cover_search(artist: str = '', query: str = ''):
        """Album-centric cover candidates (release-groups) for the art
        picker. `query` is the ALBUM (best) or the song title. Studio
        album first."""
        if not (artist.strip() or query.strip()):
            return {'covers': []}
        covers = await asyncio.get_running_loop().run_in_executor(
            None, _mb_release_group_covers, artist.strip(), query.strip())
        return {'covers': covers}

    @app.get('/api/plugins/feedpakr/caa-cover/{cover_id}')
    async def caa_cover(cover_id: str, upload_id: str = '', group: int = 0):
        """Serve the CAA front cover for a release (or release-group when
        ?group=1), cached under the upload token's temp dir. 404 when
        there's no art — the picker hides the tile."""
        entry = _uploads.get(upload_id)
        if entry is None:
            return JSONResponse({'error': 'Unknown or expired upload_id'}, 400)
        if not _CAA_ID_RE.match(cover_id or ''):
            return JSONResponse({'error': 'invalid id'}, 400)
        dest = await _caa_cached(entry, cover_id, 'release-group' if group else 'release')
        if dest is None:
            return JSONResponse({'error': 'no cover art'}, 404)
        return FileResponse(dest, media_type='image/jpeg')

    @app.post('/api/plugins/feedpakr/use-caa-cover')
    async def use_caa_cover(data: dict):
        """Pick a CAA cover as this upload's cover art: fetch/cache it and
        attach it exactly like upload-cover."""
        token = data.get('upload_id', '')
        entry = _uploads.get(token)
        if entry is None:
            return {'error': 'Unknown or expired upload_id'}
        cover_id = str((data or {}).get('release_id') or '')
        kind = 'release-group' if (data or {}).get('group') else 'release'
        if not _CAA_ID_RE.match(cover_id):
            return {'error': 'invalid id'}
        dest = await _caa_cached(entry, cover_id, kind)
        if dest is None:
            return {'error': 'no cover art'}
        entry['cover_path'] = dest
        return {'ok': True}

    @app.post('/api/plugins/feedpakr/upload-audio')
    async def upload_audio(data: dict):
        """Attach a user-supplied audio recording to an upload token, for
        the 'sync' audio mode."""
        token = data.get('upload_id', '')
        entry = _uploads.get(token)
        if entry is None:
            return {'error': 'Unknown or expired upload_id'}

        audio_bytes = _decode_upload(
            data, max_bytes=_MAX_AUDIO_BYTES,
            allowed_exts={'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'},
        )
        if isinstance(audio_bytes, dict):
            return audio_bytes

        filename = data.get('filename', 'audio.mp3')
        ext = Path(filename).suffix.lower() or '.mp3'
        audio_path = entry['dir'] / f'user_audio{ext}'
        audio_path.write_bytes(audio_bytes)
        entry['audio_path'] = audio_path
        return {'ok': True}

    @app.post('/api/plugins/feedpakr/youtube-audio')
    async def youtube_audio(data: dict):
        """Fetch a YouTube video's audio and attach it like upload-audio."""
        token = data.get('upload_id', '')
        entry = _uploads.get(token)
        if entry is None:
            return {'error': 'Unknown or expired upload_id'}

        url = (data.get('url') or '').strip()
        if not url:
            return {'error': 'No URL provided'}

        loop = asyncio.get_running_loop()
        path, err = await loop.run_in_executor(
            None, _audio.download_youtube_audio, url, str(entry['dir']),
        )
        if err:
            return {'error': err}

        entry['audio_path'] = Path(path)
        return {'ok': True}

    @app.post('/api/plugins/feedpakr/autosync-preview')
    async def autosync_preview(data: dict):
        """Run autosync against the token's attached audio and return the
        offset/sync points without building anything."""
        token = data.get('upload_id', '')
        entry = _uploads.get(token)
        if entry is None:
            return {'error': 'Unknown or expired upload_id'}
        if not entry.get('audio_path'):
            return {'error': 'No audio attached to this upload yet'}

        loop = asyncio.get_running_loop()
        offset, points, err = await loop.run_in_executor(
            None, _audio.autosync_audio,
            str(entry['gp_path']), str(entry['audio_path']),
        )
        if err:
            return {'error': err}
        return {'offset': offset, 'sync_points': points}

    @app.post('/api/plugins/feedpakr/upload-existing-pack')
    async def upload_existing_pack(data: dict):
        """Attach an existing .sloppak/.feedpak's audio to an upload token.

        Extracts every stem, the reserved 'full' mixdown (if any), and the
        cover art, so the build step can reuse them verbatim for the
        'existing_pack' audio mode instead of synthesizing/embedding/
        re-recording — re-importing the GP chart on top of a pack's audio."""
        token = data.get('upload_id', '')
        entry = _uploads.get(token)
        if entry is None:
            return {'error': 'Unknown or expired upload_id'}

        pack_bytes = _decode_upload(
            data, max_bytes=_MAX_EXISTING_PACK_BYTES,
            allowed_exts={'.sloppak', '.feedpak'},
        )
        if isinstance(pack_bytes, dict):
            return pack_bytes

        filename = data.get('filename', 'existing.feedpak')
        ext = Path(filename).suffix.lower() or '.feedpak'
        pack_path = entry['dir'] / f'existing_pack{ext}'
        pack_path.write_bytes(pack_bytes)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _upgrade.extract_pack_assets,
            pack_path, entry['dir'] / 'existing_pack_assets',
        )
        if result.get('error'):
            return {'error': result['error']}

        entry['existing_pack'] = result
        return {
            'ok': True,
            'stems': [s['id'] for s in result['stems']],
            'has_full_mix': bool(result.get('full_mix_path')),
            'has_cover': bool(result.get('cover_path')),
            'warnings': result.get('warnings', []),
        }

    @app.websocket('/ws/plugins/feedpakr/build')
    async def ws_build(
        websocket: WebSocket,
        upload_id: str,
        tracks: str = '',
        names: str = '',
        title: str = '',
        artist: str = '',
        album: str = '',
        year: str = '',
        album_artist: str = '',
        track_num: str = '',
        disc: str = '',
        genres: str = '',
        mbid: str = '',
        isrc: str = '',
        language: str = '',
        authors: str = '',
        notation_tracks: str = 'default',
        combine_same_name: bool = False,
        audio_mode: str = 'midi',
        manual_offset: str = '',
    ):
        """Build a .feedpak from the uploaded GP file, stream progress.

        tracks: comma-separated selected track indices, e.g. "0,2,3"
        names:  comma-separated "idx:Name" pairs for renamed arrangements,
                e.g. "0:Lead,2:Bass" — indices without an entry fall back
                to gp2rs's own auto-naming.
        year/track_num/disc: plain integers, or empty when unset.
        genres/authors: comma-separated lists.
        notation_tracks: literal "default" uses the pipeline default; any
                other value is a comma-separated list of track indices.
        combine_same_name: merge compatible selected tracks whose arrangement
                names match (case-insensitive).
        audio_mode: "midi" (GP3-5 FluidSynth synthesis), "embedded" (GP8's
                own backing track), "sync" (a user-uploaded or YouTube-
                fetched recording, aligned via autosync — needs
                upload-audio/youtube-audio to have been called first),
                "existing_pack" (reuse an existing .sloppak/.feedpak's
                audio/stems/cover, aligned via autosync — needs
                upload-existing-pack to have been called first), or "none".
        manual_offset: optional, "sync"/"existing_pack" only — a plain
                seconds value (may be negative) that skips autosync and
                pins the chart-to-audio offset to this number instead, for
                when the automatic alignment gets it wrong. Empty string
                (default) means "auto-detect via autosync".
        """
        await websocket.accept()

        # Validate request body upfront
        if audio_mode not in ('midi', 'embedded', 'sync', 'existing_pack', 'none'):
            await websocket.send_json({'error': f'Invalid audio_mode: {audio_mode!r}'})
            await websocket.close()
            return

        manual_offset_val: float | None = None
        if manual_offset.strip():
            try:
                manual_offset_val = float(manual_offset)
            except ValueError:
                manual_offset_val = None
            # float() also accepts 'inf'/'Infinity'/'nan' — reject those
            # too, not just unparseable strings, or a bogus offset flows
            # into the chart as a literal Infinity/NaN timestamp.
            if manual_offset_val is None or not math.isfinite(manual_offset_val):
                await websocket.send_json({
                    'error': f'manual_offset must be a finite number of seconds, got {manual_offset!r}'
                })
                await websocket.close()
                return

        try:
            track_indices = [int(x) for x in tracks.split(',') if x.strip() != '']
        except ValueError:
            await websocket.send_json({'error': 'Invalid tracks parameter'})
            await websocket.close()
            return

        if not track_indices:
            await websocket.send_json({'error': 'No tracks selected'})
            await websocket.close()
            return

        # Check for required audio dependencies upfront
        if audio_mode == 'midi':
            try:
                import gp2midi
                if gp2midi is None:
                    raise ImportError
            except ImportError:
                await websocket.send_json({
                    'error': 'Audio synthesis requires gp2midi, not installed on this host.'
                })
                await websocket.close()
                return

        if audio_mode == 'embedded':
            try:
                import gp8_audio_sync
                if gp8_audio_sync is None:
                    raise ImportError
            except ImportError:
                await websocket.send_json({
                    'error': 'Embedded audio extraction requires gp8_audio_sync, not installed on this host.'
                })
                await websocket.close()
                return

        if audio_mode == 'sync':
            # Check for ffmpeg (required for transcode_to_ogg)
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: __import__('shutil').which('ffmpeg')
            )
            if not result:
                await websocket.send_json({
                    'error': 'Autosync requires ffmpeg, not found on this host.'
                })
                await websocket.close()
                return

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
        user_audio_path = str(entry['audio_path']) if entry.get('audio_path') else None
        existing_pack = entry.get('existing_pack')
        tmp_dir = entry['dir']

        def _int_or_none(s: str) -> int | None:
            s = s.strip()
            if not s:
                return None
            try:
                return int(s)
            except ValueError:
                return None

        year_val = _int_or_none(year)
        track_val = _int_or_none(track_num)
        disc_val = _int_or_none(disc)
        genres_list = [g.strip() for g in genres.split(',') if g.strip()]
        authors_list = [a.strip() for a in authors.split(',') if a.strip()]
        try:
            notation_indices = None if notation_tracks == 'default' else {
                int(value) for value in notation_tracks.split(',') if value.strip()
            }
        except ValueError:
            await websocket.send_json({
                'error': 'notation_tracks must be "default" or comma-separated integers',
            })
            await websocket.close()
            return

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
                    year=year_val, album_artist=album_artist,
                    track=track_val, disc=disc_val,
                    genres=genres_list, mbid=mbid.strip(),
                    isrc=isrc.strip(), language=language.strip(),
                    authors=authors_list,
                    notation_track_indices=notation_indices,
                    combine_same_name=combine_same_name,
                    audio_mode=audio_mode,
                    user_audio_path=user_audio_path,
                    existing_pack=existing_pack,
                    manual_offset=manual_offset_val,
                    cover_path=cover_path,
                    report=_report,
                )

                safe_t = _pack.sanitize_filename_component(result['title'], 60)
                safe_a = _pack.sanitize_filename_component(result['artist'], 40)
                base_name = f'{safe_t}_{safe_a}' if safe_a else safe_t

                out_dir = Path(dlc) / 'feedpakr'
                out_path = _pack.unique_output_path(out_dir, base_name)
                out_path.write_bytes(result['bytes'])
                rel_name = (Path('feedpakr') / out_path.name).as_posix()

                try:
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
                    'filename_rel': rel_name,
                    'arrangement_count': result['arrangement_count'],
                    'duration': result['duration'],
                    'warnings': result['warnings'],
                    'valid': not result['validation'],
                    'features': result['features'],
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

    @app.post('/api/plugins/feedpakr/validate')
    def validate_existing_feedpak(data: dict):
        """Validate one existing DLC-relative feedpak without rebuilding it."""
        dlc = _get_dlc_dir()
        if not dlc:
            return {'error': 'DLC folder not configured'}
        rel = str((data or {}).get('path') or '').strip()
        if not rel:
            return {'error': 'path is required'}
        dlc_root = Path(dlc).resolve()
        target = (dlc_root / rel).resolve()
        try:
            target.relative_to(dlc_root)
        except ValueError:
            return {'error': 'Path escapes the DLC folder'}
        if target.suffix.lower() != '.feedpak' or not target.is_file():
            return {'error': 'Feedpak file not found'}
        try:
            report = _validate.validate_feedpak_file(target)
        except Exception as exc:
            _log.warning('feedpakr: validation failed for %r', rel, exc_info=True)
            return {'error': str(exc)}
        return {'ok': not report, 'validation': report}

    @app.get('/api/plugins/feedpakr/lyrics-text')
    def lyrics_text_for_pack(file: str = ''):
        """Reconstruct plain-text lyrics from an existing DLC-relative
        feedpak's own lyrics.json, for handing off to a forced aligner
        (lyrics_sync's /align) that wants plain text rather than pre-synced
        timing. Only meaningful for GP3-5 imports, whose build marked
        `features.lyrics_approximate` (see feedpakr_pipeline.py) — feedpakr
        itself never re-syncs anything, it just reads back what it already
        wrote so lyrics_sync has something to align against.
        """
        dlc = _get_dlc_dir()
        if not dlc:
            return JSONResponse({'error': 'DLC folder not configured'}, 400)
        rel = (file or '').strip()
        if not rel:
            return JSONResponse({'error': 'file is required'}, 400)
        dlc_root = Path(dlc).resolve()
        target = (dlc_root / rel).resolve()
        try:
            target.relative_to(dlc_root)
        except ValueError:
            return JSONResponse({'error': 'Path escapes the DLC folder'}, 400)
        if target.suffix.lower() != '.feedpak' or not target.is_file():
            return JSONResponse({'error': 'Feedpak file not found'}, 404)

        # Reuse the same optional import feedpakr_upgrade already guards
        # (host without the sloppak module installed) rather than a bare
        # import — matches how list_sloppaks reads _upgrade.sloppak_mod.
        sloppak_mod = _upgrade.sloppak_mod
        if sloppak_mod is None:
            return JSONResponse({'error': 'sloppak module not available'}, 500)
        try:
            manifest = sloppak_mod.load_manifest(target) or {}
        except Exception as exc:
            _log.warning('feedpakr: manifest read failed for %r', rel, exc_info=True)
            return JSONResponse({'error': str(exc)}, 400)
        lyrics_rel = manifest.get('lyrics')
        if not lyrics_rel:
            return JSONResponse({'error': 'This pack has no lyrics'}, 404)

        raw = sloppak_mod.read_member_bytes(target, lyrics_rel)
        if raw is None:
            return JSONResponse({'error': 'lyrics.json referenced by the manifest is missing'}, 404)
        try:
            entries = json.loads(raw.decode('utf-8'))
        except Exception:
            return JSONResponse({'error': 'lyrics.json is not valid JSON'}, 400)
        if not isinstance(entries, list):
            return JSONResponse({'error': 'lyrics.json is malformed (expected an array of entries)'}, 400)

        text = _lyrics.reconstruct_plain_text(entries)
        if not text.strip():
            return JSONResponse({'error': 'Lyrics are empty'}, 400)
        return {'lyrics_text': text}

    @app.get('/api/plugins/feedpakr/sloppaks')
    async def list_sloppaks():
        """List every .sloppak under the DLC folder for the Upgrade Library tab."""
        dlc = _get_dlc_dir()
        if not dlc:
            return {'error': 'DLC folder not configured'}

        def _scan():
            dlc_root = Path(dlc)
            entries = []
            for p in dlc_root.rglob('*.sloppak'):
                if not p.is_file():
                    continue
                rel = p.relative_to(dlc_root).as_posix()
                title, artist = '', ''
                try:
                    manifest = _upgrade.sloppak_mod.load_manifest(p) if _upgrade.sloppak_mod else None
                    if manifest:
                        title = manifest.get('title', '')
                        artist = manifest.get('artist', '')
                except Exception:
                    pass
                already = p.with_suffix('.feedpak').exists()
                entries.append({
                    'path': rel, 'title': title, 'artist': artist,
                    'already_upgraded': already,
                })
            entries.sort(key=lambda e: (e['title'] or e['path']).lower())
            return entries

        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, _scan)
        return {'sloppaks': entries}

    @app.websocket('/ws/plugins/feedpakr/upgrade')
    async def ws_upgrade(websocket: WebSocket, paths: str = ''):
        """Batch-convert selected .sloppak files (comma-separated,
        DLC-relative) to .feedpak. Originals are never touched."""
        await websocket.accept()

        dlc = _get_dlc_dir()
        if not dlc:
            await websocket.send_json({'error': 'DLC folder not configured'})
            await websocket.close()
            return

        rel_paths = [p for p in paths.split(',') if p.strip()]
        if not rel_paths:
            await websocket.send_json({'error': 'No files selected'})
            await websocket.close()
            return

        progress_queue: asyncio.Queue = asyncio.Queue()

        def _do_upgrade():
            results = []
            dlc_root = Path(dlc)
            for i, rel in enumerate(rel_paths):
                progress_queue.put_nowait({
                    'stage': f'Upgrading {rel}…',
                    'progress': int(i / len(rel_paths) * 100),
                    'current': rel,
                })
                src_path = (dlc_root / rel).resolve()
                try:
                    src_path.relative_to(dlc_root.resolve())
                except ValueError:
                    results.append({'path': rel, 'error': 'Path escapes the DLC folder'})
                    continue
                if not src_path.is_file():
                    results.append({'path': rel, 'error': 'File no longer exists'})
                    continue

                try:
                    result = _upgrade.upgrade_sloppak(str(src_path))
                    out_path = _pack.unique_output_path(
                        src_path.parent, src_path.stem, ext='.feedpak',
                    )
                    out_path.write_bytes(result['bytes'])
                    rel_out = out_path.relative_to(dlc_root).as_posix()
                    try:
                        meta = _extract_meta(out_path)
                        stat = out_path.stat()
                        _meta_db.put(rel_out, stat.st_mtime, stat.st_size, meta)
                    except Exception:
                        _log.warning('feedpakr: metadata indexing failed for %r', out_path.name, exc_info=True)
                    results.append({
                        'path': rel,
                        'output': out_path.name,
                        'output_rel': rel_out,
                        'warnings': result['warnings'],
                        'valid': not result['validation'],
                        'features': result['features'],
                    })
                except Exception as e:
                    _log.exception('feedpakr: upgrade failed for %r', rel)
                    results.append({'path': rel, 'error': str(e)})

            progress_queue.put_nowait({'done': True, 'progress': 100, 'results': results})

        loop = asyncio.get_running_loop()
        upgrade_task = loop.run_in_executor(None, _do_upgrade)

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                    await websocket.send_json(msg)
                    if msg.get('done') or msg.get('error'):
                        break
                except asyncio.TimeoutError:
                    if upgrade_task.done():
                        break
        except WebSocketDisconnect:
            pass

        await websocket.close()

    _log.info('feedpakr: routes registered')
