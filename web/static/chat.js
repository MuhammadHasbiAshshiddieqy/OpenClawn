// Logika chat single-page (index.html) — diekstrak dari inline <script> agar
// cacheable oleh browser (ui-review.md P0 #3). Data locale-aware (T, SINGLE_ROLE,
// SINGLE_TITLE, ROLES_META_BE, fillT) datang dari window.OPENCLAWN_DATA, yang
// diisi oleh blok <script> inline KECIL sebelum file ini di-load (butuh
// {{ t(...)|tojson }} server-side, jadi tak bisa diekstrak statis).
const { T, SINGLE_ROLE, SINGLE_TITLE, ROLES_META_BE, fillT } = window.OPENCLAWN_DATA;

const form = document.getElementById('chat-form');
const chatBox = document.getElementById('chat-box');
const statusLine = document.getElementById('status-line');
const textarea = form.querySelector('textarea');
const sendBtn = form.querySelector('button[type="submit"]');
const emptyState = document.getElementById('empty');
const budgetMeter = document.getElementById('budget-meter');
const budgetFill = document.getElementById('budget-fill');
const budgetLabel = document.getElementById('budget-label');

// Working directory adaptif (§ user request, ala Claude Code/OpenClaw): input
// visible di mode-bar disinkron ke hidden field yang benar-benar dikirim ke
// server (form.workdir tak boleh langsung berupa <input> terlihat karena field
// ini juga dipakai runConversation lewat body terpisah, bukan hanya submit form).
const workdirInput = document.getElementById('workdir-input');
const workdirHidden = document.getElementById('workdir-hidden');
const workdirPick = workdirInput ? workdirInput.closest('.workdir-pick') : null;
// Validasi live folder kerja: ping /workdir/check (debounced) saat user mengetik
// agar tahu SEGERA valid/tidak — bukan baru gagal di tengah turn. Kelas pada pill
// (.valid/.invalid) memberi umpan balik warna; title memuat pesan/resolved path.
let workdirTimer = null;
async function checkWorkdir() {
    if (!workdirPick) return;
    const val = workdirInput.value.trim();
    workdirPick.classList.remove('valid', 'invalid', 'checking');
    if (!val) { workdirInput.title = ''; return; }  // kosong = default server, netral
    workdirPick.classList.add('checking');
    try {
        const resp = await fetch('/workdir/check?path=' + encodeURIComponent(val));
        const data = await resp.json();
        workdirPick.classList.remove('checking');
        if (data.ok && data.resolved) {
            workdirPick.classList.add('valid');
            workdirInput.title = '✓ ' + data.resolved;
        } else if (!data.ok) {
            workdirPick.classList.add('invalid');
            workdirInput.title = data.error || '';
        }
    } catch (_) {
        workdirPick.classList.remove('checking');
    }
}
if (workdirInput && workdirHidden) {
    workdirInput.addEventListener('input', () => {
        workdirHidden.value = workdirInput.value;
        clearTimeout(workdirTimer);
        workdirTimer = setTimeout(checkWorkdir, 350);
    });
}
function currentWorkdir() { return (workdirInput && workdirInput.value.trim()) || ''; }

// Trust mode (§ user request otonomi): toggle checkbox murni, tak persist antar
// reload — harus dipilih sadar tiap sesi. Server yang menegakkan pengecualian
// (code_run tetap selalu approval, CLAUDE.md §1); ini cuma switch di form.
const trustToggle = document.getElementById('trust-toggle');
function currentTrustMode() { return !!(trustToggle && trustToggle.checked); }

// Token budget meter (§1.4): perbarui dari event usage. Warna naik ke amber/merah
// saat context mendekati batas, agar token blowout terlihat sebelum jadi masalah.
function updateBudget(u) {
    if (!u || !u.max_context_tokens) return;
    const used = u.context_tokens != null ? u.context_tokens : (u.peak_context_tokens || 0);
    if (!used) return;
    const max = u.max_context_tokens;
    const pct = Math.min(100, Math.round((used / max) * 100));
    budgetFill.style.width = pct + '%';
    budgetFill.className = 'budget-fill' + (pct >= 90 ? ' crit' : (pct >= 70 ? ' warn' : ''));
    const k = (n) => (n / 1000).toFixed(1) + 'K';
    budgetLabel.textContent = k(used) + ' / ' + k(max) + ' token (' + pct + '%)';
    budgetMeter.hidden = false;
}

// Ambang watchdog: bila TAK ada frame apa pun (token/status/ping heartbeat) selama
// jendela ini, tampilkan "masih bekerja". Dipilih > 2× interval heartbeat server
// (_HEARTBEAT_SEC=10s) agar hanya menyala saat beberapa heartbeat berturut hilang
// (lambat sungguhan), bukan saat model sekadar jeda antar-token.
const STALL_MS = 25000;

function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/\n/g, '<br>');
}

// Render markdown mentah → HTML aman (marked + DOMPurify). Fallback ke
// escape biasa bila lib gagal load, agar tetap aman dari XSS.
marked.setOptions({ breaks: true, gfm: true });
function renderMarkdown(raw) {
    try {
        return DOMPurify.sanitize(marked.parse(raw));
    } catch (_) {
        return escapeHtml(raw);
    }
}

// Label status manusiawi dari event backend.
function statusLabel(text, detail) {
    switch (text) {
        case 'routing':      return '<span class="status-tag route">' + T.statusRoute + '</span> ' + fillT(T.statusRouting, detail);
        case 'thinking':     return '<span class="status-tag think">' + T.statusThink + '</span> ' + T.statusThinking;
        case 'tool':         return '<span class="status-tag tool">' + T.statusTool + '</span> ' + detail;
        case 'tool_trusted': return '<span class="status-tag trusted">' + T.statusTrusted + '</span> ' + detail;
        case 'approval':     return '<span class="status-tag approval">' + T.statusApproval + '</span> ' + detail;
        case 'question':     return '<span class="status-tag ask">' + T.statusAsk + '</span> ' + escapeHtml(detail);
        case 'fallback':     return '<span class="status-tag fall">' + T.statusFall + '</span> ' + fillT(T.statusFallbackTo, detail);
        case 'loop_stopped': return '<span class="status-tag stop">' + T.statusStop + '</span> ' + fillT(T.statusLoopStopped, detail);
        default:             return '<span class="status-tag">' + T.statusInfo + '</span> ' + (detail ? text + ' ' + detail : text);
    }
}

// Ringkasan biaya agregat akhir percakapan (tokens/giliran/latency, cost bila >0).
function usageSummary(u) {
    if (!u || !u.turns) return '';
    const tok = (u.tokens_in || 0) + (u.tokens_out || 0);
    const secs = ((u.latency_ms || 0) / 1000).toFixed(1);
    let s = ' · ' + u.turns + ' ' + T.tokenTurns + ' · ' + tok + ' ' + T.tokenLabel + ' · ' + secs + 's';
    if (u.cost_usd && u.cost_usd > 0) s += ' · $' + u.cost_usd.toFixed(4);
    return '<span class="usage-note">' + s + '</span>';
}

// Maskot mini: menemani status-line selagi agent bekerja (playful by design).
// Murni dekoratif — tidak pernah satu-satunya penanda status (teks tetap sumber kebenaran).
const MASCOT_IMG = '<img class="mascot" src="/static/logo.png" alt="">';
function showStatus(html, kind) {
    statusLine.className = 'status-line' + (kind ? ' ' + kind : '');
    statusLine.innerHTML = MASCOT_IMG + html;
    statusLine.hidden = false;
}
function hideStatus() {
    statusLine.hidden = true;
    statusLine.innerHTML = '';
}

// Titik sisip untuk kartu action/tool/approval yang terjadi SELAMA satu giliran
// (routing/tool/approval/fallback). Dulu: selalu disisipkan sebagai sibling
// SEBELUM seluruh bubble (wrapEl) di level chatBox — akibatnya kartu yang tiba
// SETELAH thinking sudah tampil (mis. approval) tetap terlihat DI ATAS thinking,
// karena thinking ada DI DALAM bubble sedangkan kartu ada DI LUAR-nya (§ user
// report: "approval selalu muncul di atas thinking").
//
// Perbaikan: bila `wrapOrNull` adalah bubble jawaban (`.msg`) yang masih berjalan,
// kartu disisipkan DI DALAM bubble itu — SETELAH thinking-block (bila ada) tapi
// SEBELUM msg-body — sehingga urutan visual mengikuti urutan kejadian nyata:
// thinking → tool/approval → jawaban akhir, semua dalam satu bubble yang sama
// (mirip Claude Code/Claude.ai). `null`/elemen lain (mis. ringkasan akhir
// percakapan) tetap di-append ke akhir chatBox seperti sebelumnya.
function _turnInsertionPoint(wrapOrNull) {
    if (wrapOrNull && wrapOrNull.classList && wrapOrNull.classList.contains('msg')) {
        const bodyEl = wrapOrNull.querySelector('.msg-body');
        if (bodyEl) return { parent: wrapOrNull, before: bodyEl };
    }
    return { parent: chatBox, before: wrapOrNull };
}

// Action persisten: tertinggal di kolom chat sebagai jejak histori
// (routing/tool/fallback/error). Lihat _turnInsertionPoint untuk urutan.
function appendAction(html, kind, beforeEl) {
    const el = document.createElement('div');
    el.className = 'action' + (kind ? ' ' + kind : '');
    el.innerHTML = html;
    const { parent, before } = _turnInsertionPoint(beforeEl);
    parent.insertBefore(el, before);
    return el;
}

// Auto-grow textarea sesuai isi.
function autoGrow() {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 180) + 'px';
}
textarea.addEventListener('input', autoGrow);

const modeSelect = document.getElementById('mode');
const stopBtn = document.getElementById('stop-btn');
const convoHint = document.getElementById('convo-hint');
let activeConvo = null;   // AbortController saat conversation berjalan (untuk STOP + interject)
let awaitingAnswer = false;   // true saat agent menunggu jawaban ask_user (kirim ke /answer)

// ── Konfigurasi peserta percakapan ───────────────────────────────────────
const convoConfig = document.getElementById('convo-config');
const participantsBox = document.getElementById('participants');
const participantsLabel = document.getElementById('participants-label');
const roundsRow = document.getElementById('rounds-row');
const leadHintRow = document.getElementById('lead-hint-row');
const roundsVal = document.getElementById('rounds-val');
const chips = Array.from(participantsBox.querySelectorAll('.role-chip'));
let rounds = 2;

// Topbar: berganti antara konteks single-role dan label mode multi-agent.
const topbarTitle = document.getElementById('topbar-title');
const topbarSub = document.getElementById('topbar-sub');
const rolePill = document.getElementById('role-pill');
const MODE_TITLE = {
    pipeline:     ['Pipeline', T.pipelineDesc],
    debate:       ['Debate', T.debateDesc],
    orchestrator: ['Orchestrator', T.orchestratorDesc],
};

// Daftar role aktif MENGIKUTI urutan DOM chip. Untuk orchestrator, chip pertama
// yang aktif = lead (backend memakai participants[0] sebagai lead).
function activeRoles() {
    return chips.filter(c => c.classList.contains('active')).map(c => c.dataset.role);
}
// Lead = role aktif pertama dalam urutan DOM saat ini.
function leadRole() {
    const a = activeRoles();
    return a.length ? a[0] : null;
}

// Render ulang penanda ★ lead (hanya relevan untuk orchestrator).
function refreshLeadMarker() {
    const isOrch = modeSelect.value === 'orchestrator';
    const lead = leadRole();
    chips.forEach(c => {
        const star = c.querySelector('.lead-star');
        star.style.display = (isOrch && c.dataset.role === lead && c.classList.contains('active'))
            ? 'inline' : 'none';
    });
}

// Tampilkan/sembunyikan config sesuai mode + sesuaikan label & baris ronde.
function syncModeUI() {
    const mode = modeSelect.value;
    const isConvo = mode !== 'single';
    convoHint.hidden = !isConvo;
    convoConfig.hidden = !isConvo;
    roundsRow.hidden = (mode !== 'debate');
    leadHintRow.hidden = (mode !== 'orchestrator');
    participantsLabel.textContent =
        mode === 'pipeline' ? T.orderLabel :
        mode === 'orchestrator' ? T.leadWorkersLabel : T.participantsLabel;
    // Single-agent memakai sidebar "Roles" + role-pill di topbar; multi-agent
    // memakai chip peserta sendiri. Tandai <body> agar CSS meredupkan/menonaktifkan
    // pemilih role single dan topbar menampilkan label mode, bukan satu role.
    document.body.classList.toggle('mode-convo', isConvo);
    if (topbarTitle) {
        topbarTitle.textContent = isConvo ? MODE_TITLE[mode][0] : (SINGLE_TITLE[0] + ' ' + T.agentSuffix);
        topbarSub.textContent   = isConvo ? MODE_TITLE[mode][1] : SINGLE_TITLE[1];
        rolePill.textContent    = isConvo ? mode.toUpperCase() : SINGLE_ROLE.toUpperCase();
    }
    refreshLeadMarker();
}

modeSelect.addEventListener('change', syncModeUI);

// Klik chip: di orchestrator, klik chip aktif → jadikan lead (pindah ke depan).
// Klik chip non-aktif → aktifkan. Toggle nonaktif via klik kedua bila sudah lead.
chips.forEach(chip => {
    chip.addEventListener('click', () => {
        const mode = modeSelect.value;
        const isActive = chip.classList.contains('active');

        if (mode === 'orchestrator') {
            if (!isActive) {
                chip.classList.add('active');
            } else if (chip.dataset.role === leadRole()) {
                // Sudah lead & diklik lagi → nonaktifkan (kecuali ini satu-satunya).
                if (activeRoles().length > 1) chip.classList.remove('active');
            } else {
                // Aktif tapi bukan lead → jadikan lead: pindahkan ke awal kontainer.
                participantsBox.insertBefore(chip, participantsBox.firstChild);
            }
        } else {
            // pipeline / debate: toggle biasa.
            chip.classList.toggle('active');
        }
        // Sinkronkan urutan array `chips` dengan urutan DOM (penting utk lead/pipeline).
        chips.sort((a, b) =>
            Array.prototype.indexOf.call(participantsBox.children, a) -
            Array.prototype.indexOf.call(participantsBox.children, b));
        refreshLeadMarker();
    });
});

// Pemilih ronde (debate).
document.querySelectorAll('.round-btn').forEach(b => {
    b.addEventListener('click', () => {
        rounds = Math.min(5, Math.max(1, rounds + parseInt(b.dataset.delta, 10)));
        roundsVal.textContent = rounds;
    });
});

syncModeUI();

// Metadata role untuk header bubble. Label pendek dipetakan di sini; deskripsi
// diambil dari roles_meta backend (single source of truth). Role baru otomatis
// dapat header — label fallback = role uppercase.
const ROLE_LABEL = { pm: 'PM', dev: 'Dev', qa: 'QA', data: 'Data', security: 'Sec' };
const ROLE_META = {};
Object.keys(ROLES_META_BE).forEach(r => {
    ROLE_META[r] = {
        label: ROLE_LABEL[r] || r.toUpperCase(),
        desc: (ROLES_META_BE[r] && ROLES_META_BE[r][0]) || 'Agent',
    };
});

function userBubble(message) {
    const el = document.createElement('div');
    el.className = 'msg user';
    el.textContent = message;
    chatBox.appendChild(el);
    smartScroll();
}

// Buat bubble agent dengan header strip berlabel role dan body untuk konten.
// Mengembalikan elemen body (tempat innerHTML ditulis saat token masuk).
function newAssistantBubble(role) {
    const wrap = document.createElement('div');
    wrap.className = 'msg assistant md' + (role ? ' role-' + role + ' has-role' : '');

    if (role) {
        const meta = ROLE_META[role] || { label: role.toUpperCase(), desc: 'Agent' };
        const header = document.createElement('div');
        header.className = 'msg-header';
        header.innerHTML =
            '<span class="role-dot"></span>' +
            '<span class="role-name">' + meta.label + '</span>' +
            '<span class="role-desc">— ' + meta.desc + '</span>';
        wrap.appendChild(header);
    }

    const body = document.createElement('div');
    body.className = 'msg-body';
    wrap.appendChild(body);

    chatBox.appendChild(wrap);
    smartScroll();
    return body;  // caller menulis ke body, bukan ke wrap
}

// Reasoning model → blok <details> collapsible DI ATAS msg-body. Dibuat lazy
// saat thinking pertama tiba. `raw` disimpan di dataset agar bisa di-append.
function appendThinking(bodyEl, piece) {
    const wrap = bodyEl.parentElement;
    let det = wrap.querySelector('.think-block');
    if (!det) {
        det = document.createElement('details');
        det.className = 'think-block';
        det.open = true;   // terbuka selagi berpikir
        det.innerHTML = '<summary><span class="think-spin"></span> ' + T.thinkingLabel + '</summary>'
                      + '<div class="think-body"></div>';
        wrap.insertBefore(det, bodyEl);   // sebelum jawaban
        det.dataset.raw = '';
    }
    det.dataset.raw += piece;
    det.querySelector('.think-body').innerHTML = renderMarkdown(det.dataset.raw);
    smartScroll();
}

// Jawaban final mulai → tutup blok thinking & ganti label (tetap bisa dibuka lagi).
function collapseThinking(bodyEl) {
    const det = bodyEl.parentElement.querySelector('.think-block');
    if (det && det.open) {
        det.open = false;
        det.classList.add('done');
        const sum = det.querySelector('summary');
        if (sum) sum.innerHTML = T.thinkingDone;
    }
}

// Tangani satu status event → status-line efemeral atau action chip persisten.
function handleStatus(s, beforeEl) {
    if (s.text === 'question') {
        appendAction(statusLabel(s.text, s.detail), 'ask', beforeEl);
        beginAnswerMode(s.detail);
        return;
    }
    if (s.text === 'thinking') {
        showStatus(statusLabel(s.text, s.detail));
    } else if (s.text === 'tool') {
        const input = s.input || {};
        appendToolCard(s.detail, input, s.approval || '', beforeEl);
        showStatus(statusLabel(s.text, s.detail));
    } else if (s.text === 'tool_trusted') {
        // Trust mode (§ user request otonomi): tool YANG BIASANYA butuh approval
        // dieksekusi langsung. Kartu sama seperti 'tool' tapi badge 'trusted' agar
        // user tetap tahu ini aksi yang dilewati approval-nya (transparan, bukan diam-diam).
        appendToolCard(s.detail, {}, 'trusted', beforeEl);
        showStatus(statusLabel(s.text, s.detail));
    } else if (s.text === 'approval') {
        // Tool butuh persetujuan manusia SEDANG menunggu (§ chat approval UI —
        // dulu: tak ada tombol, semua tool butuh-approval selalu timeout).
        appendApprovalCard(s.detail, s.approval_id, beforeEl);
        showStatus(statusLabel(s.text, s.detail));
    } else {
        const kind = s.text === 'fallback' ? 'fallback'
                   : s.text === 'loop_stopped' ? 'error' : '';
        appendAction(statusLabel(s.text, s.detail), kind, beforeEl);
    }
}

// Mode jawab: agent menunggu jawaban ask_user. Composer dialihkan ke /answer.
function beginAnswerMode(question) {
    awaitingAnswer = true;
    textarea.placeholder = T.answerPlaceholder;
    textarea.focus();
    showStatus(statusLabel('question', question));
}
function endAnswerMode() {
    awaitingAnswer = false;
    textarea.placeholder = T.composerPlaceholder;
}

// ── Mode SINGLE: 1 agent (endpoint /chat/stream) ──────────────────────────
async function runSingle(message) {
    userBubble(message);
    showStatus('<span class="status-tag think">' + T.statusWait + '</span> ' + T.statusSending);
    const params = new URLSearchParams({ message, role: form.role.value, session_id: form.session_id.value, workdir: currentWorkdir(), trust_mode: currentTrustMode() });

    // newAssistantBubble mengembalikan body-div; wrapEl = parentElement (bubble utuh)
    const bodyEl = newAssistantBubble('');
    const wrapEl = bodyEl.parentElement;
    let raw = '';
    let watchdog;
    // Watchdog HANYA fallback: server kirim heartbeat `: ping` tiap ~10s (web/main.py
    // _with_heartbeat), yang me-reset arm() lewat readSSE('ping'). Jadi warning ini
    // baru muncul bila BEBERAPA heartbeat berturut hilang — indikasi lambat sungguhan
    // (model besar/koneksi), BUKAN "server mati". Karena itu tag 'think' (bukan 'stop'
    // merah) & teks "masih bekerja", agar user tak menyangka gagal padahal koneksi hidup.
    const arm = () => { clearTimeout(watchdog); watchdog = setTimeout(() => showStatus('<span class="status-tag think">' + T.statusWait + '</span> ' + T.statusNoResponse), STALL_MS); };

    try {
        arm();
        const resp = await fetch('/chat/stream', { method: 'POST', body: params });
        if (!resp.ok || !resp.body) { showStatus(T.errorHttp + ' ' + resp.status, 'error'); wrapEl.remove(); return; }
        await readSSE(resp, (evType, data) => {
            arm();
            if (evType === 'thinking') {
                let piece = data; try { piece = JSON.parse(data); } catch (_) {}
                appendThinking(bodyEl, piece); showStatus('<span class="status-tag think">' + T.statusThink + '</span> ' + T.statusReasoning);
            } else if (evType === 'token') {
                let piece = data; try { piece = JSON.parse(data); } catch (_) {}
                if (!raw) collapseThinking(bodyEl);
                raw += piece; bodyEl.innerHTML = renderMarkdown(raw); showStatus('<span class="status-tag think">' + T.statusGen + '</span> ' + T.statusWriting);
            } else if (evType === 'status') {
                try { handleStatus(JSON.parse(data), wrapEl); } catch (_) {}
            } else if (evType === 'usage') {
                try { updateBudget(JSON.parse(data)); } catch (_) {}
            } else if (evType === 'error') {
                let txt = data; try { txt = JSON.parse(data).text; } catch (_) {}
                appendAction('<span class="status-tag stop">' + T.statusErr + '</span> ' + escapeHtml(txt), 'error', wrapEl); showStatus(escapeHtml(txt), 'error');
            } else if (evType === 'file_created') {
                let path = data; try { path = JSON.parse(data); } catch (_) {}
                appendFileDownload(path, wrapEl);
            }
        });
        clearTimeout(watchdog);
        if (!bodyEl.innerHTML.trim()) {
            wrapEl.remove();
            if (statusLine.className.indexOf('error') === -1)
                appendAction('<span class="status-tag stop">' + T.statusErr + '</span> ' + T.errorNoAnswer, 'error', null);
        } else {
            finalizeBody(bodyEl);  // highlight + copy button setelah stream selesai
        }
        if (statusLine.className.indexOf('error') === -1) hideStatus();
    } catch (err) {
        clearTimeout(watchdog);
        appendAction('<span class="status-tag stop">' + T.statusErr + '</span> ' + T.errorDisconnected, 'error', wrapEl);
        if (!bodyEl.innerHTML.trim()) wrapEl.remove();
        hideStatus();
    } finally {
        // Sidebar riwayat (§ user report): sesi baru / judul yang baru selesai
        // di-generate perlu muncul/terupdate — refresh terlepas sukses/gagal
        // (server sudah mendaftarkan sesi di awal generate(), bahkan saat error).
        document.dispatchEvent(new CustomEvent('openclawn:turn-complete'));
    }
}

// File yang berhasil ditulis agent (file_write/file_edit/dll.) → chip download
// persisten di kolom chat, mirip appendAction tapi dengan link nyata ke
// GET /workspace/download (dibatasi ke workspace_root, lihat web/main.py).
function appendFileDownload(path, beforeEl) {
    const url = '/workspace/download?path=' + encodeURIComponent(path);
    const html =
        '<span class="status-tag file">' + T.statusFile + '</span> ' +
        '<code>' + escapeHtml(path) + '</code> ' +
        '<a href="' + url + '" download class="file-download-link">' + T.downloadFile + '</a>';
    appendAction(html, 'file', beforeEl);
}

// ── Mode CONVERSATION: banyak agent (endpoint /converse/stream) ───────────
async function runConversation(message, pattern) {
    userBubble(message);
    showStatus('<span class="status-tag route">' + T.statusStart + '</span> ' + T.statusStartingConvo);
    // Kirim peserta (urutan = lead dulu untuk orchestrator, urutan pipeline untuk pipeline)
    // + ronde (hanya dipakai backend untuk debate). Backend abaikan yang tak relevan.
    const body = new URLSearchParams({
        message, pattern,
        session_id: form.session_id.value,
        participants: activeRoles().join(','),
        rounds: String(rounds),
        workdir: currentWorkdir(),
        trust_mode: String(currentTrustMode()),
    });

    const controller = new AbortController();
    activeConvo = controller;
    stopBtn.hidden = false;
    sendBtn.hidden = true;

    let currentBody = null, currentWrap = null, currentRaw = '';
    try {
        const resp = await fetch('/converse/stream', { method: 'POST', body, signal: controller.signal });
        if (!resp.ok || !resp.body) { showStatus(T.errorHttp + ' ' + resp.status, 'error'); return; }
        await readSSE(resp, (evType, data) => {
            let d = {}; try { d = JSON.parse(data); } catch (_) {}
            if (evType === 'turn') {
                finalizeBody(currentBody);  // tuntaskan giliran sebelumnya sebelum buka bubble baru
                currentBody = newAssistantBubble(d.role);
                currentWrap = currentBody.parentElement;
                currentRaw = '';
                showStatus('<span class="status-tag think">' + T.statusGen + '</span> ' + fillT(T.statusAnswering, (d.label || d.role).toUpperCase()));
            } else if (evType === 'thinking') {
                if (!currentBody) { currentBody = newAssistantBubble(d.role); currentWrap = currentBody.parentElement; }
                appendThinking(currentBody, d.text || '');
            } else if (evType === 'token') {
                if (!currentBody) { currentBody = newAssistantBubble(d.role); currentWrap = currentBody.parentElement; }
                if (!currentRaw) collapseThinking(currentBody);   // jawaban final mulai
                currentRaw += (d.text || ''); currentBody.innerHTML = renderMarkdown(currentRaw);
            } else if (evType === 'status') {
                handleStatus(d, currentWrap);
            } else if (evType === 'conversation_end') {
                const map = {
                    stopped:       ['stop',  T.convoStopped],
                    max_turns:     ['fall',  T.convoMaxTurns],
                    strategy_done: ['route', T.convoStrategyDone],
                };
                const [cls, label] = map[d.reason] || ['', fillT(T.convoDoneGeneric, d.reason)];
                appendAction('<span class="status-tag ' + cls + '">' + label + '</span>' + usageSummary(d.usage), d.reason === 'stopped' ? 'error' : '', null);
                updateBudget(d.usage);  // peak context window lintas-giliran
            } else if (evType === 'error') {
                appendAction('<span class="status-tag stop">' + T.statusErr + '</span> ' + escapeHtml(d.text || data), 'error', null);
            } else if (evType === 'file_created') {
                appendFileDownload(d.text, currentWrap);
            }
            smartScroll();
        });
        hideStatus();
    } catch (err) {
        if (err.name === 'AbortError') {
            appendAction('<span class="status-tag stop">' + T.statusHalt + '</span> ' + T.convoStopped, 'error', null);
        } else {
            appendAction('<span class="status-tag stop">' + T.statusErr + '</span> ' + T.errorDisconnected, 'error', null);
        }
        hideStatus();
    } finally {
        finalizeBody(currentBody);  // tuntaskan giliran terakhir (sukses/abort/error)
        activeConvo = null;
        stopBtn.hidden = true;
        sendBtn.hidden = false;
    }
}

// Parser SSE bersama: panggil onFrame(evType, data) per frame; berhenti di 'done'.
async function readSSE(resp, onFrame) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '', done = false;
    while (!done) {
        const chunk = await reader.read();
        done = chunk.done;
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !done });
        let idx;
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            // Frame komentar SSE (`: ping` heartbeat server) — bukan event data.
            // Teruskan sebagai 'ping' agar caller me-reset watchdog (koneksi hidup),
            // tanpa dianggap token/status. Lihat _with_heartbeat di web/main.py.
            if (frame.startsWith(':')) { onFrame('ping', ''); continue; }
            let evType = 'message', data = '';
            for (const line of frame.split('\n')) {
                if (line.startsWith('event: ')) evType = line.slice(7);
                else if (line.startsWith('data: ')) data = line.slice(6);
            }
            if (evType === 'done') { done = true; break; }
            onFrame(evType, data);
        }
    }
}

async function sendMessage() {
    const message = textarea.value.trim();
    if (!message) return;

    // ANSWER: agent sedang menunggu jawaban ask_user → kirim ke /answer.
    // Stream yang berjalan akan lanjut sendiri begitu Future di-resolve.
    if (awaitingAnswer) {
        if (emptyState) emptyState.remove();
        textarea.value = ''; autoGrow();
        userBubble(message);
        endAnswerMode();
        const body = new URLSearchParams({ session_id: form.session_id.value, answer: message });
        fetch('/answer', { method: 'POST', body }).catch(() => {});
        showStatus('<span class="status-tag think">' + T.statusGen + '</span> ' + T.statusContinuing);
        return;
    }

    // INTERJECT: bila percakapan sedang aktif, sela alih-alih memulai baru.
    if (activeConvo) {
        if (emptyState) emptyState.remove();
        textarea.value = ''; autoGrow();
        userBubble(message);
        const body = new URLSearchParams({ session_id: form.session_id.value, message });
        fetch('/converse/interject', { method: 'POST', body }).catch(() => {});
        showStatus('<span class="status-tag fall">' + T.statusSela + '</span> ' + T.statusInterjecting);
        return;
    }

    const mode = modeSelect.value;

    // Validasi peserta untuk mode percakapan (pesan belum dikonsumsi bila gagal).
    if (mode !== 'single') {
        const n = activeRoles().length;
        if (mode === 'debate' && n < 2) {
            showStatus('<span class="status-tag stop">' + T.statusErr + '</span> ' + T.errDebateMin, 'error');
            return;
        }
        if (mode === 'orchestrator' && n < 2) {
            showStatus('<span class="status-tag stop">' + T.statusErr + '</span> ' + T.errOrchestratorMin, 'error');
            return;
        }
        if (mode === 'pipeline' && n < 1) {
            showStatus('<span class="status-tag stop">' + T.statusErr + '</span> ' + T.errPipelineMin, 'error');
            return;
        }
    }

    if (emptyState) emptyState.remove();
    textarea.value = ''; autoGrow();
    sendBtn.disabled = true;
    smartScroll();
    try {
        if (mode === 'single') await runSingle(message);
        else await runConversation(message, mode);
    } finally {
        sendBtn.disabled = false;
        textarea.focus();
    }
}

// STOP: batalkan stream (memicu is_disconnected) + endpoint cadangan.
stopBtn.addEventListener('click', function() {
    if (activeConvo) {
        const body = new URLSearchParams({ session_id: form.session_id.value });
        fetch('/converse/stop', { method: 'POST', body }).catch(() => {});
        activeConvo.abort();
    }
});

form.addEventListener('submit', function(e) {
    e.preventDefault();
    sendMessage();
});

// Enter mengirim; Shift+Enter untuk baris baru.
textarea.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// Klik chip saran → isi composer & kirim.
document.querySelectorAll('.suggest button').forEach(function(b) {
    b.addEventListener('click', function() {
        textarea.value = b.dataset.q;
        autoGrow();
        sendMessage();
    });
});

// ── Sidebar toggle ────────────────────────────────────────────────────
const sidebarToggle = document.getElementById('sidebar-toggle');
sidebarToggle.addEventListener('click', function() {
    document.body.classList.toggle('sidebar-collapsed');
    sidebarToggle.textContent = document.body.classList.contains('sidebar-collapsed') ? '▶' : '☰';
});

// ── Smart auto-scroll ─────────────────────────────────────────────────
let userScrolledUp = false;
chatBox.parentElement.addEventListener('scroll', function() {
    const s = chatBox.parentElement;
    userScrolledUp = s.scrollTop + s.clientHeight < s.scrollHeight - 60;
});
function smartScroll() {
    if (!userScrolledUp) {
        chatBox.parentElement.scrollTop = chatBox.parentElement.scrollHeight;
    }
}

// ── Copy button untuk code blocks ─────────────────────────────────────
function injectCopyButtons(el) {
    el.querySelectorAll('pre').forEach(function(pre) {
        if (pre.parentElement.classList.contains('code-block-wrap')) return;
        const wrap = document.createElement('div');
        wrap.className = 'code-block-wrap';
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = T.copyLabel;
        btn.addEventListener('click', function() {
            const code = pre.textContent || '';
            navigator.clipboard.writeText(code).then(function() {
                btn.textContent = T.copiedLabel; btn.classList.add('copied');
                setTimeout(function() { btn.textContent = T.copyLabel; btn.classList.remove('copied'); }, 1800);
            }).catch(function() {
                btn.textContent = T.copyFailed;
            });
        });
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);
        wrap.appendChild(btn);
    });
}

// ── Tool call cards ───────────────────────────────────────────────────
function appendToolCard(name, input, status, beforeEl) {
    const card = document.createElement('div');
    card.className = 'tool-card';
    // Input sebagai JSON string pendek. Tool tanpa argumen berarti (mis. list_dir
    // pada direktori saat ini) berujung {} — tampilkan bar kosong itu tak bermakna
    // bagi user, jadi baris .tc-body disembunyikan sepenuhnya kalau input kosong.
    let inputStr = '';
    const hasInput = input && typeof input === 'object' && Object.keys(input).length > 0;
    if (hasInput) {
        try { inputStr = JSON.stringify(input, null, 2); } catch (_) { inputStr = String(input); }
        if (inputStr.length > 300) inputStr = inputStr.slice(0, 300) + '…';
    }
    card.innerHTML =
        '<div class="tc-head">' +
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="4" height="4" rx="1"/><rect x="10" y="2" width="4" height="4" rx="1"/><path d="M4 6v3h2v4h4V9h2V6"/></svg>' +
        '<span class="tc-name">' + escapeHtml(name) + '</span>' +
        (status ? '<span class="tc-approval ' + status + '">' + status + '</span>' : '') +
        '</div>' +
        (hasInput ? '<div class="tc-body">' + escapeHtml(inputStr) + '</div>' : '');
    const { parent, before } = _turnInsertionPoint(beforeEl);
    parent.insertBefore(card, before);
    smartScroll();
    return card;
}

// Kartu approval interaktif: tool butuh persetujuan manusia SEDANG menunggu
// (ApprovalGate.request() blocking di backend). Dulu tak ada UI untuk ini sama
// sekali — semua tool butuh-approval (file_write/shell_run/code_run/dll.) selalu
// timeout setelah approval_timeout_sec karena user tak py cara approve. Tombol
// di sini kirim POST /approve; begitu di-resolve, stream yang sedang menunggu
// otomatis lanjut (Future di backend ter-resolve).
function appendApprovalCard(detail, approvalId, beforeEl) {
    const card = document.createElement('div');
    card.className = 'tool-card approval-pending';
    card.dataset.approvalId = approvalId;
    // `detail` datang dari backend sebagai preview "tool_name(param)" (mis.
    // "file_write(hello.go)" / "shell_run(go build)"). Pisahkan agar user melihat
    // JELAS apa yang disetujui: nama tool tebal + parameter di baris tersendiri
    // (dulu: hanya string mentah "tool(param)" di posisi nama — sulit dibaca cepat).
    const m = /^([\w.]+)\((.*)\)$/s.exec(detail || '');
    const toolName = m ? m[1] : (detail || '');
    const param = m ? m[2] : '';
    card.innerHTML =
        '<div class="tc-head">' +
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 1.5l6 3v4c0 3.5-2.5 5.5-6 6.5-3.5-1-6-3-6-6.5v-4l6-3z"/><path d="M6 8l1.5 1.5L10.5 6"/></svg>' +
        '<span class="tc-name">' + escapeHtml(toolName) + '</span>' +
        '<span class="tc-approval">' + T.statusApproval + '</span>' +
        '</div>' +
        (param ? '<div class="tc-body approval-param">' + escapeHtml(param) + '</div>' : '') +
        '<div class="tc-approval-actions">' +
        '<button type="button" class="btn-approve" data-decision="approve">' + T.approve + '</button>' +
        '<button type="button" class="btn-reject" data-decision="reject">' + T.reject + '</button>' +
        '</div>';
    const { parent, before } = _turnInsertionPoint(beforeEl);
    parent.insertBefore(card, before);
    smartScroll();

    card.querySelectorAll('button[data-decision]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
            card.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
            try {
                const resp = await fetch('/approve', {
                    method: 'POST',
                    body: new URLSearchParams({ approval_id: approvalId, decision: btn.dataset.decision }),
                });
                const data = await resp.json();
                if (data.ok) {
                    card.classList.remove('approval-pending');
                    card.classList.add('approval-' + btn.dataset.decision + 'd');
                    card.querySelector('.tc-approval-actions').innerHTML =
                        '<span class="tc-approval ' + (btn.dataset.decision === 'approve' ? 'approved' : 'rejected') + '">' +
                        (btn.dataset.decision === 'approve' ? T.approved : T.rejected) + '</span>';
                } else {
                    toast(T.approvalFailed, 'error');
                    card.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
                }
            } catch (_) {
                toast(T.approvalFailed, 'error');
                card.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
            }
        });
    });
    return card;
}

// ── Toast notifications ────────────────────────────────────────────────
let toastContainer = document.getElementById('toast-container');
if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'toast-container';
    toastContainer.id = 'toast-container';
    document.body.appendChild(toastContainer);
}
function toast(msg, kind) {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    toastContainer.appendChild(el);
    setTimeout(function() {
        el.classList.add('removing');
        setTimeout(function() { el.remove(); }, 200);
    }, 2800);
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────
document.addEventListener('keydown', function(e) {
    if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
    // Ctrl+K: fokus composer
    if (e.ctrlKey && e.key === 'k') { e.preventDefault(); textarea.focus(); }
    // Escape: blur composer
    if (e.key === 'Escape') { textarea.blur(); }
});

// ── Drag & drop file ke composer ───────────────────────────────────────
// Browser File tidak punya .path (itu API Electron); baca ISI file via
// FileReader lalu sisipkan ke composer. Tidak auto-send: user meninjau
// dulu (file besar/biner di-tolak dengan pesan jelas).
const DROP_MAX_BYTES = 256 * 1024;  // 256 KiB — cukup untuk teks, cegah blow-up context
const dropIndicator = document.createElement('div');
dropIndicator.className = 'drop-indicator';
dropIndicator.innerHTML = '<div>📂 ' + T.dropHint + '</div>';
document.body.appendChild(dropIndicator);

// preventDefault HANYA saat seret membawa file — biarkan drag teks biasa
// ke textarea bekerja normal.
function dragHasFiles(e) {
    const t = e.dataTransfer;
    return t && t.types && Array.prototype.indexOf.call(t.types, 'Files') !== -1;
}
let dragCounter = 0;
document.addEventListener('dragenter', function(e) {
    if (!dragHasFiles(e)) return;
    e.preventDefault(); dragCounter++; dropIndicator.classList.add('show');
});
document.addEventListener('dragleave', function(e) {
    if (!dragHasFiles(e)) return;
    e.preventDefault(); dragCounter--;
    if (dragCounter <= 0) { dragCounter = 0; dropIndicator.classList.remove('show'); }
});
document.addEventListener('dragover', function(e) { if (dragHasFiles(e)) e.preventDefault(); });
document.addEventListener('drop', function(e) {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragCounter = 0;
    dropIndicator.classList.remove('show');
    const file = e.dataTransfer.files[0];
    if (!file) return;
    if (file.size > DROP_MAX_BYTES) {
        toast(fillT(T.fileTooLarge, DROP_MAX_BYTES / 1024), 'error');
        return;
    }
    const reader = new FileReader();
    reader.onload = function() {
        const content = String(reader.result || '');
        const sep = textarea.value.trim() ? '\n\n' : '';
        textarea.value += sep + T.fileContentLabel + ' `' + file.name + '`:\n\n```\n' + content + '\n```\n';
        autoGrow();
        textarea.focus();
        toast(fillT(T.fileInserted, file.name), 'success');
    };
    reader.onerror = function() { toast(T.fileReadFailed, 'error'); };
    reader.readAsText(file);
});

// ── Syntax highlight + copy button: dijalankan SEKALI setelah giliran
// selesai stream (bukan tiap token). MutationObserver per-mutasi dibuang
// karena streaming me-rewrite innerHTML berulang — observer tak terpicu
// (node msg-body tak baru) dan boros saat subtree dipantau penuh.
function finalizeBody(bodyEl) {
    if (!bodyEl) return;
    injectCopyButtons(bodyEl);
    if (typeof hljs !== 'undefined') {
        bodyEl.querySelectorAll('pre code').forEach(function(b) {
            try { hljs.highlightElement(b); } catch (_) {}
        });
    }
    // Maskot cheer: HANYA saat bubble benar-benar berisi jawaban (bukan
    // dipanggil pada path abort/error dengan body kosong/sebagian). Murni
    // dekoratif — tidak pernah dipakai sebagai penanda status di tempat lain.
    if (bodyEl.textContent.trim()) {
        const cheer = document.createElement('img');
        cheer.src = '/static/logo.png'; cheer.alt = ''; cheer.className = 'mascot-cheer';
        bodyEl.appendChild(cheer);
    }
}

// ── Riwayat chat (§ user report: chat selalu ke-reset, tak ada cara buka chat
// baru/lanjutkan/hapus riwayat) ─────────────────────────────────────────────
//
// Akar masalah lama: session_id di-generate ULANG (uuid server) tiap halaman
// di-load — tak pernah disimpan di browser, jadi refresh selalu terasa seperti
// chat baru walau transkrip (session_turns) sudah tersimpan di DB. Perbaikan:
// simpan session_id AKTIF di localStorage, kirim balik lewat hidden field yang
// sudah ada (form.session_id) — tak perlu ubah endpoint /chat/stream sama sekali.
const LS_SESSION_KEY = 'openclawn_active_session';
const historyListEl = document.getElementById('chat-history-list');
const newChatBtn = document.getElementById('new-chat-btn');

function currentSessionId() { return form.session_id.value; }
function setSessionId(id) {
    form.session_id.value = id;
    try { localStorage.setItem(LS_SESSION_KEY, id); } catch (_) { /* privasi/incognito: no-op */ }
}

// Saat halaman dimuat: pakai session_id tersimpan (bila ada) alih-alih yang
// di-generate server di setiap render — inilah yang membuat refresh TIDAK lagi
// memulai sesi baru. localStorage bisa gagal (privasi/incognito) → fail-safe
// ke uuid server (perilaku lama, sesi baru tiap load, bukan crash).
(function restoreActiveSession() {
    try {
        const saved = localStorage.getItem(LS_SESSION_KEY);
        if (saved) form.session_id.value = saved;
        else localStorage.setItem(LS_SESSION_KEY, form.session_id.value);
    } catch (_) { /* no-op, pakai uuid server apa adanya */ }
})();

// "Chat baru": session_id baru (client-side, cukup unik untuk tujuan ini —
// server hanya butuh string unik per sesi, tak perlu UUID kripto) DISIMPAN ke
// localStorage lalu halaman di-reload. Reload (bukan bersih-bersih DOM manual)
// dipilih sengaja: state UI lain (mode select, convo config, empty-state
// bawaan template) ikut ter-reset bersih & konsisten, bukan direkonstruksi
// manual di JS yang rawan meleset dari markup server. restoreActiveSession()
// di awal file otomatis memakai session_id baru ini setelah reload.
function generateClientSessionId() {
    return 'sess-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}
function startNewChat() {
    setSessionId(generateClientSessionId());
    location.reload();
}
if (newChatBtn) newChatBtn.addEventListener('click', startNewChat);

// Muat transkrip sesi lama dari riwayat → render ulang bubble user/assistant,
// lalu jadikan sesi itu aktif (pesan baru berikutnya melanjutkan sesi ini).
async function loadChatSession(sessionId) {
    let data;
    try {
        const resp = await fetch('/chat-sessions/' + encodeURIComponent(sessionId) + '/turns');
        data = await resp.json();
    } catch (_) {
        toast(T.errorDisconnected, 'error');
        return;
    }
    if (emptyState) emptyState.remove();
    chatBox.querySelectorAll('.msg, .action, .tool-card').forEach(function(el) { el.remove(); });
    (data.turns || []).forEach(function(turn) {
        if (turn.role === 'user') {
            userBubble(turn.content);
        } else {
            const body = newAssistantBubble('');
            body.innerHTML = renderMarkdown(turn.content || '');
            finalizeBody(body);
        }
    });
    setSessionId(sessionId);
    highlightActiveHistoryItem();
    smartScroll();
}

async function deleteChatSession(sessionId, itemEl) {
    if (!confirm(T.historyDeleteConfirm)) return;
    try {
        const resp = await fetch('/chat-sessions/' + encodeURIComponent(sessionId), { method: 'DELETE' });
        const data = await resp.json();
        if (!data.ok) throw new Error('delete failed');
    } catch (_) {
        toast(T.errorDisconnected, 'error');
        return;
    }
    if (itemEl) itemEl.remove();
    // Sesi aktif dihapus → mulai chat baru (tak ada transkrip lagi untuk ditampilkan).
    if (sessionId === currentSessionId()) startNewChat();
}

function highlightActiveHistoryItem() {
    if (!historyListEl) return;
    const active = currentSessionId();
    historyListEl.querySelectorAll('.history-item').forEach(function(el) {
        el.classList.toggle('active', el.dataset.sessionId === active);
    });
}

const BUCKET_LABELS = { today: 'bucketToday', yesterday: 'bucketYesterday', '7d': 'bucket7d', '30d': 'bucket30d', older: 'bucketOlder' };
const BUCKET_ORDER = ['today', 'yesterday', '7d', '30d', 'older'];

// Render daftar riwayat: dikelompokkan GANDA (§ user request) — heading per
// bucket WAKTU (urutan tetap), lalu di dalam tiap bucket dikelompokkan lagi
// per ROLE (label kecil, bukan heading terpisah — biar tak terlalu berlapis).
function renderChatHistory(sessions) {
    if (!historyListEl) return;
    const render = function(list) {
        historyListEl.innerHTML = '';
        if (!list.length) {
            const empty = document.createElement('div');
            empty.className = 'history-empty';
            empty.textContent = T.historyEmpty;
            historyListEl.appendChild(empty);
            return;
        }
        const byBucket = {};
        list.forEach(function(s) { (byBucket[s.bucket] = byBucket[s.bucket] || []).push(s); });
        BUCKET_ORDER.forEach(function(bucket) {
            const items = byBucket[bucket];
            if (!items || !items.length) return;
            const heading = document.createElement('div');
            heading.className = 'history-bucket-label';
            heading.textContent = T[BUCKET_LABELS[bucket]];
            historyListEl.appendChild(heading);
            items.forEach(function(s) {
                const item = document.createElement('div');
                item.className = 'history-item' + (s.session_id === currentSessionId() ? ' active' : '');
                item.dataset.sessionId = s.session_id;
                item.innerHTML =
                    '<span class="history-role-dot" title="' + escapeHtml(s.role.toUpperCase()) + '"></span>' +
                    '<span class="history-title">' + escapeHtml(s.title) + '</span>' +
                    '<button type="button" class="history-delete" title="' + escapeHtml(T.historyDelete) + '">' +
                    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 4.5h10M6.5 4.5V3a1 1 0 011-1h1a1 1 0 011 1v1.5M4.5 4.5l.6 8.4a1 1 0 001 .9h3.8a1 1 0 001-.9l.6-8.4"/></svg>' +
                    '</button>';
                item.classList.add('role-' + s.role);
                item.addEventListener('click', function(e) {
                    if (e.target.closest('.history-delete')) return;
                    loadChatSession(s.session_id);
                });
                item.querySelector('.history-delete').addEventListener('click', function(e) {
                    e.stopPropagation();
                    deleteChatSession(s.session_id, item);
                });
                historyListEl.appendChild(item);
            });
        });
    };
    if (sessions) { render(sessions); return; }
    fetch('/chat-sessions').then(function(r) { return r.json(); }).then(function(data) {
        render(data.sessions || []);
    }).catch(function() { /* sidebar riwayat opsional — kegagalan diam-diam, chat tetap jalan */ });
}
renderChatHistory();
// Refresh daftar tiap kali turn selesai (judul baru/sesi baru muncul) — event
// custom di-dispatch dari sendMessage/runSingle/runConversation setelah selesai.
document.addEventListener('openclawn:turn-complete', function() { renderChatHistory(); });
