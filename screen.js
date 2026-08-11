// feedpakr plugin — screen.js

(function () {
'use strict';

const PLUGIN_ID = 'feedpakr';
const API_BASE  = `/api/plugins/${PLUGIN_ID}`;
const WS_PROTO  = location.protocol === 'https:' ? 'wss' : 'ws';
const WS_BASE   = `${WS_PROTO}://${location.host}/ws/plugins/${PLUGIN_ID}`;

const GP345_EXTS = ['gp3', 'gp4', 'gp5'];
const GPX_EXTS   = ['gpx', 'gp'];
const ALL_EXTS   = GP345_EXTS.concat(GPX_EXTS);

let _uploadId = null;
let _tracks = [];
let _buildDone = false;
let _format = 'gp345';
let _hasEmbeddedAudio = false;
let _audioAttached = false;
let _existingPackAttached = false;
let _sloppaks = [];
let _upgradeDone = false;
let _handoffAvailability = { preview: null, split: null, lyricsSync: null, difficulty: null };

function esc(s) {
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Drop zone setup ───────────────────────────────────────────────────────

setTimeout(() => {
    const dropzone  = document.getElementById('fpr-dropzone');
    const fileInput = document.getElementById('fpr-file-input');
    if (!dropzone || !fileInput) return;

    // Guard against a click on an inner button (e.g. "Try another file"
    // rendered into #fpr-dropzone-prompt after an error) also bubbling up
    // and re-opening the file picker underneath it.
    dropzone.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        fileInput.click();
    });

    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('border-accent/60', 'bg-accent/5');
    });

    dropzone.addEventListener('dragleave', () => {
        dropzone.classList.remove('border-accent/60', 'bg-accent/5');
    });

    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('border-accent/60', 'bg-accent/5');
        const file = e.dataTransfer.files[0];
        if (file) fprHandleFile(file);
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files[0]) fprHandleFile(fileInput.files[0]);
    });

    const coverInput = document.getElementById('fpr-cover-input');
    if (coverInput) {
        coverInput.addEventListener('change', () => {
            if (coverInput.files[0]) fprHandleCover(coverInput.files[0]);
        });
    }

    const audioFileInput = document.getElementById('fpr-audio-file-input');
    if (audioFileInput) {
        audioFileInput.addEventListener('change', () => {
            if (audioFileInput.files[0]) fprHandleAudioFile(audioFileInput.files[0]);
        });
    }

    const existingPackInput = document.getElementById('fpr-existing-pack-input');
    if (existingPackInput) {
        existingPackInput.addEventListener('change', () => {
            if (existingPackInput.files[0]) fprHandleExistingPack(existingPackInput.files[0]);
        });
    }

    for (const radio of document.querySelectorAll('input[name="fpr-audio-mode"]')) {
        radio.addEventListener('change', fprUpdateAudioModeUI);
    }

    for (const radio of document.querySelectorAll('input[name="fpr-sync-method"]')) {
        radio.addEventListener('change', fprUpdateSyncMethodUI);
    }
}, 100);

// ── File handling ─────────────────────────────────────────────────────────

function fprHandleFile(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    const prompt = document.getElementById('fpr-dropzone-prompt');

    if (!ALL_EXTS.includes(ext)) {
        alert('Only .gp3, .gp4, .gp5, .gpx, and .gp files are supported.');
        return;
    }

    prompt.innerHTML = `<p class="text-gray-400 text-sm">Parsing ${esc(file.name)}…</p>`;

    const reader = new FileReader();
    reader.onload = async (e) => {
        const b64 = e.target.result.split(',')[1];
        try {
            const resp = await fetch(`${API_BASE}/upload`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename: file.name, data: b64 }),
            });
            const data = await resp.json();

            if (data.error) {
                prompt.innerHTML = `
                    <p class="text-red-400 text-sm">${esc(data.error)}</p>
                    <button onclick="fprReset()" class="mt-3 text-xs text-gray-500 hover:text-white">
                        Try another file
                    </button>`;
                return;
            }

            _uploadId = data.upload_id;
            fprShowParsed(data);
        } catch (err) {
            prompt.innerHTML = `
                <p class="text-red-400 text-sm">Upload failed: ${esc(String(err))}</p>
                <button onclick="fprReset()" class="mt-3 text-xs text-gray-500 hover:text-white">
                    Try again
                </button>`;
        }
    };
    reader.readAsDataURL(file);
}

async function fprHandleAudioFile(file) {
    if (!_uploadId) return;
    const status = document.getElementById('fpr-sync-status');
    if (status) status.textContent = `Uploading ${file.name}…`;
    const reader = new FileReader();
    reader.onload = async (e) => {
        const b64 = e.target.result.split(',')[1];
        try {
            const resp = await fetch(`${API_BASE}/upload-audio`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: _uploadId, filename: file.name, data: b64 }),
            });
            const data = await resp.json();
            if (data.error) {
                if (status) status.textContent = `Audio upload failed: ${data.error}`;
                return;
            }
            _audioAttached = true;
            // Force the mode radio to "sync" — otherwise a build fired while
            // some other mode is still checked (whatever it defaulted to)
            // silently ignores this attached audio entirely instead of
            // erroring, since the build-time guard only checks the reverse
            // case (sync selected, nothing attached).
            const syncRadio = document.querySelector('input[name="fpr-audio-mode"][value="sync"]');
            if (syncRadio) { syncRadio.checked = true; fprUpdateAudioModeUI(); }
            if (status) status.textContent = `${file.name} attached — will be aligned to the chart during build.`;
        } catch (err) {
            if (status) status.textContent = `Audio upload failed: ${String(err)}`;
        }
    };
    reader.readAsDataURL(file);
}

async function fprHandleExistingPack(file) {
    if (!_uploadId) return;
    const status = document.getElementById('fpr-existing-pack-status');
    if (status) status.textContent = `Uploading ${file.name}…`;
    const reader = new FileReader();
    reader.onload = async (e) => {
        const b64 = e.target.result.split(',')[1];
        try {
            const resp = await fetch(`${API_BASE}/upload-existing-pack`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: _uploadId, filename: file.name, data: b64 }),
            });
            const data = await resp.json();
            if (data.error) {
                if (status) status.textContent = `Upload failed: ${data.error}`;
                return;
            }
            _existingPackAttached = true;
            // Same reasoning as fprHandleAudioFile: force the matching mode
            // radio so this attached pack can't silently go unused under
            // whatever mode happened to be checked before the upload
            // completed.
            const existingRadio = document.querySelector('input[name="fpr-audio-mode"][value="existing_pack"]');
            if (existingRadio) { existingRadio.checked = true; fprUpdateAudioModeUI(); }
            const stemNote = data.stems.length ? `${data.stems.length} stem(s)` : null;
            const mixNote = data.has_full_mix ? 'full mix' : null;
            const parts = [stemNote, mixNote].filter(Boolean).join(' + ') || 'audio';
            if (status) status.textContent = `${file.name} attached (${parts}) — will be aligned to the new chart during build.`;
        } catch (err) {
            if (status) status.textContent = `Upload failed: ${String(err)}`;
        }
    };
    reader.readAsDataURL(file);
}

async function fprFetchYoutube() {
    if (!_uploadId) return;
    const urlInput = document.getElementById('fpr-youtube-url');
    const url = urlInput && urlInput.value.trim();
    if (!url) return;

    const status = document.getElementById('fpr-sync-status');
    if (status) status.textContent = 'Fetching audio from YouTube…';
    try {
        const resp = await fetch(`${API_BASE}/youtube-audio`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ upload_id: _uploadId, url }),
        });
        const data = await resp.json();
        if (data.error) {
            if (status) status.textContent = `YouTube fetch failed: ${data.error}`;
            return;
        }
        _audioAttached = true;
        // Same reasoning as fprHandleAudioFile: force "sync" so this fetched
        // audio can't silently go unused under whatever mode happened to be
        // checked before the fetch completed.
        const syncRadio = document.querySelector('input[name="fpr-audio-mode"][value="sync"]');
        if (syncRadio) { syncRadio.checked = true; fprUpdateAudioModeUI(); }
        if (status) status.textContent = 'Audio fetched — will be aligned to the chart during build.';
    } catch (err) {
        if (status) status.textContent = `YouTube fetch failed: ${String(err)}`;
    }
}

// ── Album cover search (MusicBrainz release-groups + Cover Art Archive) ────
// Same mechanism as the editor plugin's cover picker: search by artist +
// album/title, studio albums sorted first, click a tile to fetch/cache/attach.
async function fprSearchCover() {
    if (!_uploadId) return;
    const artist = (document.getElementById('fpr-artist').value || '').trim();
    const album = (document.getElementById('fpr-album').value || '').trim();
    const title = (document.getElementById('fpr-title').value || '').trim();
    const query = album || title;
    const status = document.getElementById('fpr-cover-status');
    const results = document.getElementById('fpr-cover-results');

    if (!artist && !query) {
        if (status) status.textContent = 'Fill in artist and/or album first.';
        return;
    }
    if (status) status.textContent = 'Searching MusicBrainz…';
    results.classList.add('hidden');
    results.classList.remove('grid', 'grid-cols-4');
    results.innerHTML = '';

    try {
        const params = new URLSearchParams({ artist, query });
        const resp = await fetch(`${API_BASE}/cover-search?${params}`);
        const data = await resp.json();
        const covers = data.covers || [];
        if (!covers.length) {
            if (status) status.textContent = 'No candidates found — try adjusting artist/album, or upload a file instead.';
            return;
        }
        if (status) status.textContent = `${covers.length} candidate(s) — click one to use it:`;
        results.classList.remove('hidden');
        results.classList.add('grid', 'grid-cols-4');
        for (const c of covers) {
            const tile = document.createElement('button');
            tile.type = 'button';
            tile.className = 'relative aspect-square rounded-lg overflow-hidden border border-gray-700 hover:border-accent transition bg-dark-600';
            tile.title = `${c.title || 'Untitled'}${c.year ? ' (' + c.year + ')' : ''}${c.studio ? '' : ' — non-studio release'}`;
            const img = document.createElement('img');
            img.className = 'w-full h-full object-cover';
            img.loading = 'lazy';
            img.src = `${API_BASE}/caa-cover/${encodeURIComponent(c.id)}?upload_id=${encodeURIComponent(_uploadId)}&group=1`;
            img.onerror = () => { tile.remove(); };
            tile.appendChild(img);
            tile.onclick = () => fprUseCover(c.id);
            results.appendChild(tile);
        }
    } catch (err) {
        if (status) status.textContent = `Search failed: ${String(err)}`;
    }
}

async function fprUseCover(releaseGroupId) {
    if (!_uploadId) return;
    const status = document.getElementById('fpr-cover-status');
    if (status) status.textContent = 'Fetching cover…';
    try {
        const resp = await fetch(`${API_BASE}/use-caa-cover`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ upload_id: _uploadId, release_id: releaseGroupId, group: true }),
        });
        const data = await resp.json();
        if (data.error) {
            if (status) status.textContent = `Couldn't use that cover: ${data.error}`;
            return;
        }
        if (status) status.textContent = 'Cover attached — will be baked into the pack.';
    } catch (err) {
        if (status) status.textContent = `Failed to attach cover: ${String(err)}`;
    }
}

// Returns { offset, error }: offset is the manual seconds value as a string
// ('' when auto-detect applies or no mode needs it), error is a message to
// show the caller when the user picked manual but didn't enter a valid
// number. Split out of fprBuild to keep that function's branch count down.
function fprCollectManualOffset(audioMode) {
    if (audioMode !== 'sync' && audioMode !== 'existing_pack') return { offset: '', error: null };
    const syncMethodInput = document.querySelector('input[name="fpr-sync-method"]:checked');
    const syncMethod = syncMethodInput ? syncMethodInput.value : 'auto';
    if (syncMethod !== 'manual') return { offset: '', error: null };
    const raw = (document.getElementById('fpr-manual-offset')?.value || '').trim();
    if (raw === '' || !Number.isFinite(Number(raw))) {
        return { offset: '', error: 'Enter a numeric manual offset in seconds, or switch back to auto-detect.' };
    }
    return { offset: raw, error: null };
}

function fprUpdateSyncMethodUI() {
    const selected = document.querySelector('input[name="fpr-sync-method"]:checked');
    const method = selected ? selected.value : 'auto';
    const row = document.getElementById('fpr-manual-offset-row');
    if (row) row.classList.toggle('hidden', method !== 'manual');
}

function fprUpdateAudioModeUI() {
    const selected = document.querySelector('input[name="fpr-audio-mode"]:checked');
    const mode = selected ? selected.value : 'midi';
    const syncControls = document.getElementById('fpr-sync-controls');
    if (syncControls) syncControls.classList.toggle('hidden', mode !== 'sync');
    const existingPackControls = document.getElementById('fpr-existing-pack-controls');
    if (existingPackControls) existingPackControls.classList.toggle('hidden', mode !== 'existing_pack');
    const syncMethodControls = document.getElementById('fpr-sync-method-controls');
    if (syncMethodControls) {
        syncMethodControls.classList.toggle('hidden', mode !== 'sync' && mode !== 'existing_pack');
    }

    // Grey out (rather than hide) modes the uploaded file can't support,
    // so the UI explains itself instead of options silently vanishing.
    const midiRow = document.querySelector('[data-audio-mode-row="midi"]');
    const embeddedRow = document.querySelector('[data-audio-mode-row="embedded"]');
    if (midiRow) {
        const midiRadio = midiRow.querySelector('input');
        const disabled = _format === 'gpif';
        midiRadio.disabled = disabled;
        midiRow.classList.toggle('text-gray-700', disabled);
        if (disabled && midiRadio.checked) {
            const fallback = document.querySelector(
                `input[name="fpr-audio-mode"][value="${_hasEmbeddedAudio ? 'embedded' : 'sync'}"]`
            );
            if (fallback) { fallback.checked = true; fprUpdateAudioModeUI(); }
        }
    }
    if (embeddedRow) {
        const embeddedRadio = embeddedRow.querySelector('input');
        embeddedRadio.disabled = !_hasEmbeddedAudio;
        embeddedRow.classList.toggle('text-gray-700', !_hasEmbeddedAudio);
    }
}

async function fprHandleCover(file) {
    if (!_uploadId) return;
    const reader = new FileReader();
    reader.onload = async (e) => {
        const b64 = e.target.result.split(',')[1];
        try {
            const resp = await fetch(`${API_BASE}/upload-cover`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_id: _uploadId, filename: file.name, data: b64 }),
            });
            const data = await resp.json();
            if (data.error) alert(`Cover art failed: ${data.error}`);
        } catch (err) {
            alert(`Cover art upload failed: ${String(err)}`);
        }
    };
    reader.readAsDataURL(file);
}

function fprShowParsed(data) {
    document.getElementById('fpr-dropzone').classList.add('hidden');
    document.getElementById('fpr-parsed').classList.remove('hidden');
    document.getElementById('fpr-progress').classList.add('hidden');
    document.getElementById('fpr-result').classList.add('hidden');

    document.getElementById('fpr-title').value  = data.title  || '';
    document.getElementById('fpr-artist').value = data.artist || '';
    document.getElementById('fpr-album').value  = data.album  || '';

    _format = data.format || 'gp345';
    _hasEmbeddedAudio = !!data.has_embedded_audio;
    _audioAttached = false;
    _existingPackAttached = false;
    const defaultMode = document.querySelector(
        `input[name="fpr-audio-mode"][value="${_format === 'gpif' ? (_hasEmbeddedAudio ? 'embedded' : 'sync') : 'midi'}"]`
    );
    if (defaultMode) defaultMode.checked = true;
    const autoMethodRadio = document.querySelector('input[name="fpr-sync-method"][value="auto"]');
    if (autoMethodRadio) autoMethodRadio.checked = true;
    const manualOffsetInput = document.getElementById('fpr-manual-offset');
    if (manualOffsetInput) manualOffsetInput.value = '';
    fprUpdateAudioModeUI();
    fprUpdateSyncMethodUI();

    _tracks = data.tracks || [];
    const container = document.getElementById('fpr-tracks');
    container.innerHTML = _tracks.map((t) => {
        const badge = t.is_vocal ? '🎤' : t.is_drums ? '🥁' : t.is_piano ? '🎹' : t.is_bass ? '🎸' : '🎸';
        return `
        <div class="flex items-center gap-3 py-2 border-b border-gray-800 last:border-0" data-track-row="${t.index}">
            <input type="checkbox" data-track-check="${t.index}" ${t.auto_selected ? 'checked' : ''}
                class="accent-blue-500 shrink-0">
            <span class="text-sm w-5 text-center shrink-0">${badge}</span>
            <span class="text-sm text-gray-300 flex-1 truncate">${esc(t.name || `Track ${t.index + 1}`)}</span>
            <span class="text-xs text-gray-600 shrink-0">${t.strings}str · ${t.notes} notes</span>
            <input type="text" data-track-name="${t.index}" value="${esc(t.auto_name || '')}"
                placeholder="Arrangement name"
                class="w-32 bg-dark-600 border border-gray-700 rounded-lg px-2 py-1 text-xs text-gray-200 outline-none focus:border-accent/50 shrink-0">
            ${_format === 'gpif' && !t.is_vocal && !t.is_drums ? `
            <label class="text-xs text-gray-500 shrink-0" title="Generate a standard-notation sidecar">
                <input type="checkbox" data-track-notation="${t.index}" ${t.is_piano ? 'checked' : ''}
                    class="accent-blue-500"> Notation
            </label>` : ''}
        </div>`;
    }).join('') || '<p class="text-xs text-gray-600">No tracks found.</p>';
}

// ── Build ─────────────────────────────────────────────────────────────────

function fprCollectTracks() {
    const indices = [];
    const names = [];
    const notation = [];
    for (const t of _tracks) {
        const check = document.querySelector(`[data-track-check="${t.index}"]`);
        if (!check || !check.checked) continue;
        indices.push(t.index);
        const nameInput = document.querySelector(`[data-track-name="${t.index}"]`);
        const name = (nameInput && nameInput.value.trim()) || '';
        if (name) names.push(`${t.index}:${name}`);
        const notationCheck = document.querySelector(`[data-track-notation="${t.index}"]`);
        if (notationCheck?.checked) notation.push(t.index);
    }
    return { tracks: indices.join(','), names: names.join(','), notation: notation.join(',') };
}

function fprBuild() {
    if (!_uploadId) return;
    const { tracks, names, notation } = fprCollectTracks();
    if (!tracks) {
        alert('Select at least one track to import.');
        return;
    }

    const title  = document.getElementById('fpr-title').value.trim();
    const artist = document.getElementById('fpr-artist').value.trim();
    const album  = document.getElementById('fpr-album').value.trim();
    // Extended metadata (feedpak spec §5.1) — all optional, same field set
    // as the editor plugin's create-modal "Song details" panel.
    const albumArtist = (document.getElementById('fpr-album-artist')?.value || '').trim();
    const year = (document.getElementById('fpr-year')?.value || '').trim();
    const track = (document.getElementById('fpr-track')?.value || '').trim();
    const disc = (document.getElementById('fpr-disc')?.value || '').trim();
    const genres = (document.getElementById('fpr-genres')?.value || '').trim();
    const language = (document.getElementById('fpr-language')?.value || '').trim();
    const isrc = (document.getElementById('fpr-isrc')?.value || '').trim();
    const mbid = (document.getElementById('fpr-mbid')?.value || '').trim();
    const authors = (document.getElementById('fpr-authors')?.value || '').trim();
    const audioModeInput = document.querySelector('input[name="fpr-audio-mode"]:checked');
    const audioMode = audioModeInput ? audioModeInput.value : 'midi';
    const combineSameName = document.getElementById('fpr-combine-same-name')?.checked ? 'true' : 'false';

    if (audioMode === 'sync' && !_audioAttached) {
        alert('Attach a recording (file upload or YouTube fetch) before building in sync mode, or pick a different audio option.');
        return;
    }
    if (audioMode === 'existing_pack' && !_existingPackAttached) {
        alert('Attach an existing .sloppak/.feedpak before building in re-chart mode, or pick a different audio option.');
        return;
    }

    const { offset: manualOffset, error: manualOffsetError } = fprCollectManualOffset(audioMode);
    if (manualOffsetError) {
        alert(manualOffsetError);
        return;
    }

    document.getElementById('fpr-parsed').classList.add('hidden');
    document.getElementById('fpr-progress').classList.remove('hidden');
    document.getElementById('fpr-result').classList.add('hidden');
    document.getElementById('fpr-bar').style.width = '0%';
    document.getElementById('fpr-stage').textContent = 'Starting…';

    _buildDone = false;
    const params = new URLSearchParams({
        upload_id: _uploadId, tracks, names, title, artist, album,
        album_artist: albumArtist, year, track_num: track, disc,
        genres, language, isrc, mbid, authors,
        notation_tracks: _format === 'gpif' ? notation : 'default',
        combine_same_name: combineSameName,
        audio_mode: audioMode,
        manual_offset: manualOffset,
    });
    const ws = new WebSocket(`${WS_BASE}/build?${params}`);

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);

        if (msg.progress !== undefined)
            document.getElementById('fpr-bar').style.width = msg.progress + '%';
        if (msg.stage)
            document.getElementById('fpr-stage').textContent = msg.stage;

        if (msg.done) {
            _buildDone = true;
            fprShowResult(msg);
        }
        if (msg.error) {
            _buildDone = true;
            fprShowError(msg.error);
        }
    };

    ws.onerror = () => {
        if (_buildDone) return;
        _buildDone = true;
        fprShowError('Connection error.');
    };

    // A clean server-side close with no done/error frame would otherwise
    // leave the progress bar stuck forever — surface it instead.
    ws.onclose = () => {
        if (_buildDone) return;
        _buildDone = true;
        fprShowError('Connection closed unexpectedly before the build finished.');
    };
}

// ── Handoffs to sibling plugins (when installed) ───────────────────────────
// Sibling plugins expose same-origin REST endpoints — feedpakr only probes
// for their presence and calls them; it never generates previews, splits
// stems, or authors difficulty ladders itself.

async function fprProbeHandoffs() {
    if (_handoffAvailability.preview === null) {
        try {
            const r = await fetch('/api/plugins/song_preview/audit');
            _handoffAvailability.preview = r.ok;
        } catch (err) { _handoffAvailability.preview = false; }
    }
    if (_handoffAvailability.split === null) {
        try {
            const r = await fetch('/api/plugins/stem_splitter/jobs');
            _handoffAvailability.split = r.ok;
        } catch (err) { _handoffAvailability.split = false; }
    }
    if (_handoffAvailability.lyricsSync === null) {
        try {
            const r = await fetch('/api/plugins/lyrics_sync/status');
            _handoffAvailability.lyricsSync = r.ok;
        } catch { _handoffAvailability.lyricsSync = false; }
    }
    if (_handoffAvailability.difficulty === null) {
        try {
            const r = await fetch('/api/plugins/difficulty_ladder/generate', { method: 'OPTIONS' });
            _handoffAvailability.difficulty = r.status !== 404;
        } catch (err) { _handoffAvailability.difficulty = false; }
    }
}

function fprHandoffButtonsHtml(relPath, allowSplit = true, allowLyricsSync = false, allowDifficulty = true) {
    if (!relPath) return '';
    return `<div class="flex items-center gap-2 mt-2" data-handoff-for="${esc(relPath)}">
        <button data-handoff="preview" data-handoff-path="${esc(relPath)}"
            class="hidden px-3 py-1 rounded-lg text-xs text-gray-300 bg-dark-600 hover:bg-dark-500 transition">
            Generate Preview
        </button>
        <button data-handoff="split" data-handoff-path="${esc(relPath)}" data-split-allowed="${allowSplit ? '1' : '0'}"
            class="hidden px-3 py-1 rounded-lg text-xs text-gray-300 bg-dark-600 hover:bg-dark-500 transition">
            Split Stems
        </button>
        <button data-handoff="lyrics-sync" data-handoff-path="${esc(relPath)}" data-lyrics-sync-allowed="${allowLyricsSync ? '1' : '0'}"
            class="hidden px-3 py-1 rounded-lg text-xs text-gray-300 bg-dark-600 hover:bg-dark-500 transition">
            Sync Lyrics
        </button>
        <button data-handoff="difficulty" data-handoff-path="${esc(relPath)}" data-difficulty-allowed="${allowDifficulty ? '1' : '0'}"
            class="hidden px-3 py-1 rounded-lg text-xs text-gray-300 bg-dark-600 hover:bg-dark-500 transition">
            Generate Difficulty Ladder
        </button>
        <span data-handoff-status class="text-xs text-gray-500"></span>
    </div>`;
}

async function fprWireHandoffButtons(container) {
    await fprProbeHandoffs();
    container.querySelectorAll('[data-handoff="preview"]').forEach((btn) => {
        if (!_handoffAvailability.preview) return;
        btn.classList.remove('hidden');
        btn.addEventListener('click', () => fprRunHandoff(btn, 'preview'));
    });
    container.querySelectorAll('[data-handoff="split"]').forEach((btn) => {
        if (!_handoffAvailability.split || btn.getAttribute('data-split-allowed') !== '1') return;
        btn.classList.remove('hidden');
        btn.addEventListener('click', () => fprRunHandoff(btn, 'split'));
    });
    container.querySelectorAll('[data-handoff="lyrics-sync"]').forEach((btn) => {
        if (!_handoffAvailability.lyricsSync || btn.getAttribute('data-lyrics-sync-allowed') !== '1') return;
        btn.classList.remove('hidden');
        btn.addEventListener('click', () => fprRunLyricsSyncHandoff(btn));
    });
    container.querySelectorAll('[data-handoff="difficulty"]').forEach((btn) => {
        if (!_handoffAvailability.difficulty || btn.getAttribute('data-difficulty-allowed') !== '1') return;
        btn.classList.remove('hidden');
        btn.addEventListener('click', () => fprRunHandoff(btn, 'difficulty'));
    });
}

function fprHandoffStatus(kind, phase, data) {
    if (kind === 'preview') return phase === 'start' ? 'Generating preview…' : 'Preview generated.';
    if (kind === 'split') return phase === 'start' ? 'Requesting stem split…' : `Stem split queued (${(data && data.enqueued) || 1} job).`;
    if (kind === 'difficulty') {
        if (phase === 'start') return 'Generating difficulty ladder…';
        if (data && data.skipped === 'already-has-phrases') return 'Difficulty ladder already exists.';
        return `Difficulty ladder generated${data && data.phrases ? ` (${data.phrases} phrase(s))` : ''}.`;
    }
    return phase === 'start' ? 'Working…' : 'Done.';
}

function fprHandoffRequest(kind, relPath) {
    if (kind === 'preview') {
        return fetch(`/api/plugins/song_preview/backfill?file=${encodeURIComponent(relPath)}`, { method: 'POST' });
    }
    if (kind === 'split') {
        return fetch('/api/plugins/stem_splitter/split', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: relPath }),
        });
    }
    if (kind === 'difficulty') {
        return fetch('/api/plugins/difficulty_ladder/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: relPath, arrangement_index: 0, force: false }),
        });
    }
    throw new Error(`Unknown handoff: ${kind}`);
}

async function fprRunHandoff(btn, kind) {
    const relPath = btn.getAttribute('data-handoff-path');
    const statusEl = btn.parentElement.querySelector('[data-handoff-status]');
    btn.disabled = true;
    if (statusEl) statusEl.textContent = fprHandoffStatus(kind, 'start');

    try {
        const resp = await fprHandoffRequest(kind, relPath);
        const data = await resp.json();
        if (data.error || data.ok === false) {
            if (statusEl) statusEl.textContent = data.error || "Needs setup — check the plugin's Settings page.";
            btn.disabled = false;
        } else {
            if (statusEl) statusEl.textContent = fprHandoffStatus(kind, 'done', data);
            btn.classList.add('hidden');
        }
    } catch (err) {
        if (statusEl) statusEl.textContent = `Failed: ${String(err)}`;
        btn.disabled = false;
    }
}

// Unlike preview/split (one fetch, feedpakr never touches the result),
// lyrics_sync needs feedpakr to (1) hand back the pack's own approximate
// lyrics as plain text — lyrics_sync's /align takes text, not pre-synced
// entries — then (2) forward the aligned segments it returns straight
// into lyrics_sync's own /save. feedpakr still never does the syncing
// itself; it's just relaying its own already-written data through.
async function fprRunLyricsSyncHandoff(btn) {
    const relPath = btn.getAttribute('data-handoff-path');
    const statusEl = btn.parentElement.querySelector('[data-handoff-status]');
    btn.disabled = true;
    const setStatus = (text) => { if (statusEl) statusEl.textContent = text; };

    try {
        setStatus('Reading existing lyrics…');
        const textResp = await fetch(`/api/plugins/feedpakr/lyrics-text?file=${encodeURIComponent(relPath)}`);
        const textData = await textResp.json();
        if (!textResp.ok || textData.error) {
            setStatus(textData.error || 'Could not read this pack’s lyrics.');
            btn.disabled = false;
            return;
        }

        setStatus('Aligning against the vocals stem…');
        const alignResp = await fetch('/api/plugins/lyrics_sync/align', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: relPath, lyrics_text: textData.lyrics_text }),
        });
        const alignData = await alignResp.json();
        if (!alignResp.ok || alignData.error || !Array.isArray(alignData.segments)) {
            setStatus(alignData.error || "Needs setup — check lyrics_sync's Settings page.");
            btn.disabled = false;
            return;
        }

        setStatus('Saving synced lyrics…');
        const saveResp = await fetch('/api/plugins/lyrics_sync/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: relPath, segments: alignData.segments }),
        });
        const saveData = await saveResp.json();
        if (!saveResp.ok || saveData.error) {
            setStatus(saveData.error || 'Failed to save synced lyrics.');
            btn.disabled = false;
            return;
        }

        setStatus(`Lyrics re-synced (${saveData.lyrics_count || alignData.segments.length} line(s)).`);
        btn.classList.add('hidden');
    } catch (err) {
        setStatus(`Failed: ${String(err)}`);
        btn.disabled = false;
    }
}

function fprShowResult(msg) {
    document.getElementById('fpr-progress').classList.add('hidden');
    document.getElementById('fpr-result').classList.remove('hidden');

    const mins = Math.floor((msg.duration || 0) / 60);
    const secs = Math.round((msg.duration || 0) % 60);

    const warnings = msg.warnings || [];
    const warningsHtml = warnings.length
        ? `<div class="text-left mt-3 bg-dark-600 rounded-lg p-3">
               <p class="text-xs text-amber-400/80 mb-1">${warnings.length} note(s):</p>
               <ul class="text-xs text-gray-500 list-disc list-inside space-y-0.5">
                   ${warnings.map((w) => `<li>${esc(w)}</li>`).join('')}
               </ul>
           </div>`
        : '';

    const validityBadge = msg.valid
        ? '<span class="text-green-400 text-xs">✓ Passes spec validation</span>'
        : '<span class="text-amber-400/80 text-xs">⚠ Some parts did not validate — see notes below</span>';

    const f = msg.features || {};
    const featureLabels = [
        f.song_timeline && 'sections/beats',
        f.keys && 'key signature',
        f.lyrics && 'lyrics',
        f.vocal_pitch && 'vocal pitch',
        f.handshapes && 'chord shapes',
        f.tones && 'tone changes',
        f.drum_arrangements ? `${f.drum_arrangements} drum arrangement(s)` : null,
        f.notation ? `${f.notation} notation part(s)` : null,
    ].filter(Boolean);
    const featuresHtml = featureLabels.length
        ? `<p class="text-xs text-gray-500 mt-2">Captured: ${featureLabels.map(esc).join(' · ')}</p>`
        : '';

    document.getElementById('fpr-result').innerHTML = `
        <div class="bg-green-900/20 border border-green-800/30 rounded-xl p-5 text-center">
            <p class="text-green-400 font-semibold mb-1">Feedpak created!</p>
            <p class="text-sm text-gray-400">${esc(msg.filename)}</p>
            <p class="text-xs text-gray-500 mt-1">
                ${msg.arrangement_count} arrangement(s) &nbsp;·&nbsp; ${mins}:${String(secs).padStart(2, '0')}
            </p>
            <p class="mt-2">${validityBadge}</p>
            ${featuresHtml}
            ${warningsHtml}
            ${fprHandoffButtonsHtml(msg.filename_rel, !!f.real_audio && !f.already_separated, !!(f.lyrics_approximate && f.real_audio), !f.phrase_ladder)}
            <button onclick="fprReset()"
                class="mt-4 px-4 py-2 bg-dark-600 hover:bg-dark-500 rounded-xl text-sm text-gray-300 transition">
                Import Another
            </button>
        </div>`;

    fprWireHandoffButtons(document.getElementById('fpr-result'));
}

function fprShowError(message) {
    document.getElementById('fpr-progress').classList.add('hidden');
    document.getElementById('fpr-result').classList.remove('hidden');
    document.getElementById('fpr-result').innerHTML = `
        <div class="bg-red-900/20 border border-red-800/30 rounded-xl p-5 text-center">
            <p class="text-red-400 font-semibold mb-1">Build Failed</p>
            <p class="text-sm text-gray-400">${esc(message)}</p>
            <button onclick="fprReset()"
                class="mt-4 px-4 py-2 bg-dark-600 hover:bg-dark-500 rounded-xl text-sm text-gray-300 transition">
                Try Again
            </button>
        </div>`;
}

// ── Reset ─────────────────────────────────────────────────────────────────

function fprReset() {
    _uploadId = null;
    _tracks = [];
    _buildDone = false;
    _format = 'gp345';
    _hasEmbeddedAudio = false;
    _audioAttached = false;
    _existingPackAttached = false;
    const fi = document.getElementById('fpr-file-input');
    if (fi) fi.value = '';
    const ci = document.getElementById('fpr-cover-input');
    if (ci) ci.value = '';
    const afi = document.getElementById('fpr-audio-file-input');
    if (afi) afi.value = '';
    const epi = document.getElementById('fpr-existing-pack-input');
    if (epi) epi.value = '';
    const yu = document.getElementById('fpr-youtube-url');
    if (yu) yu.value = '';
    const ss = document.getElementById('fpr-sync-status');
    if (ss) ss.textContent = '';
    const eps = document.getElementById('fpr-existing-pack-status');
    if (eps) eps.textContent = '';
    const midiRadio = document.querySelector('input[name="fpr-audio-mode"][value="midi"]');
    if (midiRadio) midiRadio.checked = true;
    fprUpdateAudioModeUI();

    document.getElementById('fpr-parsed').classList.add('hidden');
    document.getElementById('fpr-progress').classList.add('hidden');
    document.getElementById('fpr-result').classList.add('hidden');

    document.getElementById('fpr-dropzone').classList.remove('hidden');

    // #fpr-file-input is never touched by this reset (see the comment on
    // the dropzone markup in screen.html) — only its prompt content needs
    // restoring, so the load-time click/change listeners stay valid.
    document.getElementById('fpr-dropzone-prompt').innerHTML = `
        <svg class="w-12 h-12 mx-auto mb-4 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"/>
        </svg>
        <p class="text-gray-400 text-sm mb-2">Drag and drop a Guitar Pro file here</p>
        <p class="text-gray-600 text-xs">or click to browse &nbsp;·&nbsp; .gp3 .gp4 .gp5 .gp6 .gpx .gp</p>`;
}

// ── Tabs ─────────────────────────────────────────────────────────────────

function fprShowTab(tab) {
    document.getElementById('fpr-tab-import').classList.toggle('hidden', tab !== 'import');
    document.getElementById('fpr-tab-upgrade').classList.toggle('hidden', tab !== 'upgrade');
    document.querySelectorAll('[data-tab-button]').forEach((btn) => {
        const active = btn.getAttribute('data-tab-button') === tab;
        btn.classList.toggle('border-accent', active);
        btn.classList.toggle('text-white', active);
        btn.classList.toggle('border-transparent', !active);
        btn.classList.toggle('text-gray-500', !active);
    });
    if (tab === 'upgrade' && !_sloppaks.length) fprRefreshSloppaks();
}

// ── Upgrade Library ──────────────────────────────────────────────────────

async function fprRefreshSloppaks() {
    const list = document.getElementById('fpr-sloppak-list');
    list.innerHTML = '<p class="text-xs text-gray-600">Loading…</p>';
    try {
        const resp = await fetch(`${API_BASE}/sloppaks`);
        const data = await resp.json();
        if (data.error) {
            list.innerHTML = `<p class="text-xs text-red-400">${esc(data.error)}</p>`;
            return;
        }
        _sloppaks = data.sloppaks || [];
        if (!_sloppaks.length) {
            list.innerHTML = '<p class="text-xs text-gray-600">No .sloppak files found under the DLC folder.</p>';
            return;
        }
        list.innerHTML = _sloppaks.map((s) => `
            <div class="flex items-center gap-3 py-2 border-b border-gray-800 last:border-0">
                <input type="checkbox" data-sloppak-check="${esc(s.path)}" class="accent-blue-500 shrink-0"
                    ${s.already_upgraded ? '' : 'checked'}>
                <span class="text-sm text-gray-300 flex-1 truncate">
                    ${esc(s.title || s.path)}${s.artist ? ' — ' + esc(s.artist) : ''}
                </span>
                <span class="text-xs text-gray-600 shrink-0 truncate" style="max-width:16rem">${esc(s.path)}</span>
                ${s.already_upgraded ? '<span class="text-xs text-green-400 shrink-0">already upgraded</span>' : ''}
            </div>`).join('');
    } catch (err) {
        list.innerHTML = `<p class="text-xs text-red-400">Failed to load: ${esc(String(err))}</p>`;
    }
}

function fprSelectAllSloppaks(state) {
    document.querySelectorAll('[data-sloppak-check]').forEach((cb) => { cb.checked = state; });
}

async function fprUpgradeSelected() {
    const paths = Array.from(document.querySelectorAll('[data-sloppak-check]'))
        .filter((cb) => cb.checked)
        .map((cb) => cb.getAttribute('data-sloppak-check'));
    if (!paths.length) {
        alert('Select at least one file to upgrade.');
        return;
    }

    document.getElementById('fpr-upgrade-progress').classList.remove('hidden');
    document.getElementById('fpr-upgrade-result').classList.add('hidden');
    document.getElementById('fpr-upgrade-bar').style.width = '0%';
    document.getElementById('fpr-upgrade-stage').textContent = 'Starting…';

    _upgradeDone = false;
    const params = new URLSearchParams({ paths: paths.join(',') });
    const ws = new WebSocket(`${WS_BASE}/upgrade?${params}`);

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.progress !== undefined)
            document.getElementById('fpr-upgrade-bar').style.width = msg.progress + '%';
        if (msg.stage)
            document.getElementById('fpr-upgrade-stage').textContent = msg.stage;

        if (msg.done) {
            _upgradeDone = true;
            fprShowUpgradeResults(msg.results || []);
        }
        if (msg.error) {
            _upgradeDone = true;
            document.getElementById('fpr-upgrade-progress').classList.add('hidden');
            document.getElementById('fpr-upgrade-result').classList.remove('hidden');
            document.getElementById('fpr-upgrade-result').innerHTML =
                `<p class="text-red-400 text-sm">${esc(msg.error)}</p>`;
        }
    };

    ws.onerror = () => {
        if (_upgradeDone) return;
        _upgradeDone = true;
        document.getElementById('fpr-upgrade-progress').classList.add('hidden');
        document.getElementById('fpr-upgrade-result').classList.remove('hidden');
        document.getElementById('fpr-upgrade-result').innerHTML =
            '<p class="text-red-400 text-sm">Connection error.</p>';
    };

    // Same rationale as the import build's ws.onclose: a clean server-side
    // close with no done/error frame would otherwise leave the bar stuck.
    ws.onclose = () => {
        if (_upgradeDone) return;
        _upgradeDone = true;
        document.getElementById('fpr-upgrade-progress').classList.add('hidden');
        document.getElementById('fpr-upgrade-result').classList.remove('hidden');
        document.getElementById('fpr-upgrade-result').innerHTML =
            '<p class="text-red-400 text-sm">Connection closed unexpectedly before the upgrade finished.</p>';
    };
}

function fprShowUpgradeResults(results) {
    document.getElementById('fpr-upgrade-progress').classList.add('hidden');
    document.getElementById('fpr-upgrade-result').classList.remove('hidden');

    const rows = results.map((r) => {
        if (r.error) {
            return `<div class="py-2 border-b border-gray-800 last:border-0">
                <p class="text-sm text-gray-300">${esc(r.path)}</p>
                <p class="text-xs text-red-400">${esc(r.error)}</p>
            </div>`;
        }
        const badge = r.valid
            ? '<span class="text-green-400 text-xs">✓ valid</span>'
            : '<span class="text-amber-400/80 text-xs">⚠ issues</span>';
        const warns = (r.warnings || []).length
            ? `<ul class="text-xs text-gray-500 list-disc list-inside space-y-0.5 mt-1">
                   ${r.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}
               </ul>`
            : '';
        return `<div class="py-2 border-b border-gray-800 last:border-0">
            <p class="text-sm text-gray-300">${esc(r.output)} ${badge}</p>
            ${warns}
            ${fprHandoffButtonsHtml(r.output_rel, !!r.features?.real_audio, !r.features?.phrase_ladder)}
        </div>`;
    }).join('');

    document.getElementById('fpr-upgrade-result').innerHTML = `
        <div class="bg-dark-700 border border-gray-800 rounded-xl p-4">
            ${rows}
        </div>
        <button onclick="fprRefreshSloppaks()"
            class="mt-4 px-4 py-2 bg-dark-600 hover:bg-dark-500 rounded-xl text-sm text-gray-300 transition">
            Refresh List
        </button>`;

    fprWireHandoffButtons(document.getElementById('fpr-upgrade-result'));
}

// Expose handlers globally so onclick= in screen.html works
window.fprBuild = fprBuild;
window.fprReset = fprReset;
window.fprFetchYoutube = fprFetchYoutube;
window.fprSearchCover = fprSearchCover;
window.fprShowTab = fprShowTab;
window.fprRefreshSloppaks = fprRefreshSloppaks;
window.fprSelectAllSloppaks = fprSelectAllSloppaks;
window.fprUpgradeSelected = fprUpgradeSelected;

})();
