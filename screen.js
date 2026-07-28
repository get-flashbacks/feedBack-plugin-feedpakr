// feedpakr plugin — screen.js

(function () {
'use strict';

const PLUGIN_ID = 'feedpakr';
const API_BASE  = `/api/plugins/${PLUGIN_ID}`;
const WS_PROTO  = location.protocol === 'https:' ? 'wss' : 'ws';
const WS_BASE   = `${WS_PROTO}://${location.host}/ws/plugins/${PLUGIN_ID}`;

const GP345_EXTS = ['gp3', 'gp4', 'gp5'];
const GPX_EXTS   = ['gpx', 'gp'];

let _uploadId = null;
let _tracks = [];
let _buildDone = false;

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
}, 100);

// ── File handling ─────────────────────────────────────────────────────────

function fprHandleFile(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    const prompt = document.getElementById('fpr-dropzone-prompt');

    if (GPX_EXTS.includes(ext)) {
        prompt.innerHTML = `
            <p class="text-amber-400/80 text-sm">GP6/GP7/GP8 (.${esc(ext)}) support is coming in a later update.</p>
            <p class="text-gray-600 text-xs mt-2">Today's importer handles .gp3, .gp4, and .gp5 files.</p>
            <button onclick="fprReset()" class="mt-3 text-xs text-gray-500 hover:text-white">
                Try another file
            </button>`;
        return;
    }
    if (!GP345_EXTS.includes(ext)) {
        alert('Only .gp3, .gp4, and .gp5 files are supported.');
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

    _tracks = data.tracks || [];
    const container = document.getElementById('fpr-tracks');
    container.innerHTML = _tracks.map((t) => {
        const badge = t.is_drums ? '🥁' : t.is_piano ? '🎹' : t.is_bass ? '🎸' : '🎸';
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
    const includeAudio = document.getElementById('fpr-include-audio');

    document.getElementById('fpr-parsed').classList.add('hidden');
    document.getElementById('fpr-progress').classList.remove('hidden');
    document.getElementById('fpr-result').classList.add('hidden');
    document.getElementById('fpr-bar').style.width = '0%';
    document.getElementById('fpr-stage').textContent = 'Starting…';

    _buildDone = false;
    const params = new URLSearchParams({
        upload_id: _uploadId, tracks, names, title, artist, album,
        audio: (!includeAudio || includeAudio.checked) ? '1' : '0',
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
    const fi = document.getElementById('fpr-file-input');
    if (fi) fi.value = '';
    const ci = document.getElementById('fpr-cover-input');
    if (ci) ci.value = '';
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
        <p class="text-gray-600 text-xs">or click to browse &nbsp;·&nbsp; .gp3 .gp4 .gp5</p>`;
}

// Expose handlers globally so onclick= in screen.html works
window.fprBuild = fprBuild;
window.fprReset = fprReset;

})();
