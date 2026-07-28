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

    for (const radio of document.querySelectorAll('input[name="fpr-audio-mode"]')) {
        radio.addEventListener('change', fprUpdateAudioModeUI);
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
            if (status) status.textContent = `${file.name} attached — will be aligned to the chart during build.`;
        } catch (err) {
            if (status) status.textContent = `Audio upload failed: ${String(err)}`;
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
        if (status) status.textContent = 'Audio fetched — will be aligned to the chart during build.';
    } catch (err) {
        if (status) status.textContent = `YouTube fetch failed: ${String(err)}`;
    }
}

function fprUpdateAudioModeUI() {
    const selected = document.querySelector('input[name="fpr-audio-mode"]:checked');
    const mode = selected ? selected.value : 'midi';
    const syncControls = document.getElementById('fpr-sync-controls');
    if (syncControls) syncControls.classList.toggle('hidden', mode !== 'sync');

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
    const defaultMode = document.querySelector(
        `input[name="fpr-audio-mode"][value="${_format === 'gpif' ? (_hasEmbeddedAudio ? 'embedded' : 'sync') : 'midi'}"]`
    );
    if (defaultMode) defaultMode.checked = true;
    fprUpdateAudioModeUI();

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
        </div>`;
    }).join('') || '<p class="text-xs text-gray-600">No tracks found.</p>';
}

// ── Build ─────────────────────────────────────────────────────────────────

function fprCollectTracks() {
    const indices = [];
    const names = [];
    for (const t of _tracks) {
        const check = document.querySelector(`[data-track-check="${t.index}"]`);
        if (!check || !check.checked) continue;
        indices.push(t.index);
        const nameInput = document.querySelector(`[data-track-name="${t.index}"]`);
        const name = (nameInput && nameInput.value.trim()) || '';
        if (name) names.push(`${t.index}:${name}`);
    }
    return { tracks: indices.join(','), names: names.join(',') };
}

async function fprBuild() {
    if (!_uploadId) return;
    const { tracks, names } = fprCollectTracks();
    if (!tracks) {
        alert('Select at least one track to import.');
        return;
    }

    const title  = document.getElementById('fpr-title').value.trim();
    const artist = document.getElementById('fpr-artist').value.trim();
    const album  = document.getElementById('fpr-album').value.trim();
    const audioModeInput = document.querySelector('input[name="fpr-audio-mode"]:checked');
    const audioMode = audioModeInput ? audioModeInput.value : 'midi';

    if (audioMode === 'sync' && !_audioAttached) {
        alert('Attach a recording (file upload or YouTube fetch) before building in sync mode, or pick a different audio option.');
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
        audio_mode: audioMode,
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

    document.getElementById('fpr-result').innerHTML = `
        <div class="bg-green-900/20 border border-green-800/30 rounded-xl p-5 text-center">
            <p class="text-green-400 font-semibold mb-1">Feedpak created!</p>
            <p class="text-sm text-gray-400">${esc(msg.filename)}</p>
            <p class="text-xs text-gray-500 mt-1">
                ${msg.arrangement_count} arrangement(s) &nbsp;·&nbsp; ${mins}:${String(secs).padStart(2, '0')}
            </p>
            <p class="mt-2">${validityBadge}</p>
            ${warningsHtml}
            <button onclick="fprReset()"
                class="mt-4 px-4 py-2 bg-dark-600 hover:bg-dark-500 rounded-xl text-sm text-gray-300 transition">
                Import Another
            </button>
        </div>`;
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
    const fi = document.getElementById('fpr-file-input');
    if (fi) fi.value = '';
    const ci = document.getElementById('fpr-cover-input');
    if (ci) ci.value = '';
    const afi = document.getElementById('fpr-audio-file-input');
    if (afi) afi.value = '';
    const yu = document.getElementById('fpr-youtube-url');
    if (yu) yu.value = '';
    const ss = document.getElementById('fpr-sync-status');
    if (ss) ss.textContent = '';
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

// Expose handlers globally so onclick= in screen.html works
window.fprBuild = fprBuild;
window.fprReset = fprReset;
window.fprFetchYoutube = fprFetchYoutube;

})();
