-- migrations/001_initial.sql

-- ===================== MEMORY =====================
CREATE TABLE IF NOT EXISTS memory_l1 (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL,
    key TEXT NOT NULL, value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(role, key)
);

CREATE TABLE IF NOT EXISTS memory_l2 (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL,
    fact TEXT NOT NULL, importance INTEGER DEFAULT 1,
    locale TEXT DEFAULT 'neutral',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_l2_role ON memory_l2(role, importance DESC);

-- Transkrip percakapan PER-SESI untuk single-agent chat. AgentLoop dibuat baru
-- tiap request web (self.history selalu kosong di awal), sehingga sebelumnya turn
-- N+1 tak pernah melihat turn N — model menganggap "konteks kurang" walau di sesi
-- yang sama (§ user report). Tabel ini menyimpan tiap giliran (user/assistant)
-- ber-session_id lalu dimuat kembali ke self.history di awal run(). Dipisah dari
-- conversations (multi-agent) karena granularitasnya per-turn, bukan per-transkrip.
CREATE TABLE IF NOT EXISTS session_turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,        -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_turns ON session_turns(session_id, id);

-- Folder kerja aktif PER-SESI, bisa diubah agent sendiri lewat tool set_workdir
-- (§ user request: "pindah direktori secara dinamis" via chat, bukan cuma field
-- UI). Satu baris per sesi (UPSERT) — beda dari session_turns yang append-log,
-- ini state "saat ini", bukan riwayat. Dibaca di awal AgentLoop.run() SEBELUM
-- workspace_override dari form UI (override eksplisit form tetap menang bila
-- diisi user secara manual di UI pada request itu — lihat core/agent_loop.py).
CREATE TABLE IF NOT EXISTS session_workspace (
    session_id TEXT PRIMARY KEY,
    workdir TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Metadata sesi chat single-agent untuk sidebar riwayat (§ user report: "chat
-- selalu ke-reset", tak ada cara membuka chat baru/lanjutkan/hapus riwayat).
-- Terpisah dari session_turns (transkrip per-giliran) — ini metadata TAMPILAN
-- (judul, kapan dibuat/terakhir aktif, role) untuk daftar di sidebar. `title`
-- NULL sampai turn pertama selesai & judul di-generate LLM lokal (async,
-- tak menghambat jawaban pertama user). `deleted_at` soft-delete (bukan DELETE
-- fisik) agar audit trail approval_log/tool_invocations lama tetap konsisten
-- referensinya — baris session_turns terkait TETAP dihapus fisik saat user
-- menghapus riwayat (lihat ChatSessionStore.delete), hanya metadata ini yang soft.
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_active
    ON chat_sessions(deleted_at, updated_at DESC);

-- ===================== SKILLS + DECAY [#2] =====================
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL,
    skill_name TEXT NOT NULL, trigger_pattern TEXT, skill_content TEXT NOT NULL,
    visibility TEXT DEFAULT 'private',     -- private | shared | inherited
    status TEXT DEFAULT 'active',          -- active | draft | archived | merged
    confidence REAL DEFAULT 0.0,
    generator_model TEXT,                  -- model yang menghasilkan [#3, untuk evaluator gating]
    use_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    decay_score REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Compounding intelligence: I1 curator (merge) + I3 refine (version).
    merged_into INTEGER REFERENCES skills(id),  -- I1: skill yang menyerap isi ini (status='merged')
    version INTEGER NOT NULL DEFAULT 1,          -- I3: dinaikkan tiap refine/merge
    draft_success_count INTEGER NOT NULL DEFAULT 0,  -- I2: draft naik 'active' setelah N sukses
    UNIQUE(role, skill_name)
);
CREATE INDEX IF NOT EXISTS idx_skills_active ON skills(role, status, decay_score DESC);

-- ===================== SESSION ARCHIVE (FTS5) =====================
CREATE VIRTUAL TABLE IF NOT EXISTS memory_l4 USING fts5(
    role, session_id, summary, full_content, created_at UNINDEXED
);

-- ===================== ROUTING AUDIT [#1] =====================
CREATE TABLE IF NOT EXISTS routing_events (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    user_id TEXT DEFAULT 'default',        -- [Audit log format actor_is_agent, § Prioritas 2] AgentConfig.user_id, query-able terpisah dari session_id (integrasi SIEM eksternal)
    actor_is_agent INTEGER DEFAULT 1,      -- selalu 1 di sini (baris ini SELALU tindakan agent) — eksplisit karena pola GitHub control plane mengharapkan field ini, bukan diasumsikan
    query_text TEXT NOT NULL,
    dim_query_tokens INTEGER, dim_has_tech_kw INTEGER, dim_needs_multistep INTEGER,
    dim_history_len INTEGER, dim_role TEXT, dim_has_urgency INTEGER,
    dim_needs_stream INTEGER, dim_is_continuation INTEGER,
    dim_soul_upgrade_hit INTEGER,          -- [v0.3] keyword dari soul.toml cocok?
    complexity_score INTEGER, complexity_label TEXT,
    model_chosen TEXT, provider TEXT, routing_reason TEXT,
    fallback_used INTEGER DEFAULT 0,        -- [v0.3] apakah fallback chain terpakai?
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, latency_ms INTEGER,
    had_correction INTEGER DEFAULT 0, correction_detail TEXT,
    evidence_json TEXT,                    -- [Evidence-Based Response] snapshot policy/skill/guardrail, query-able via GET /evidence/{id}
    human_feedback INTEGER,                -- [Runtime Evaluation Engine] rating eksplisit user 1-5 via POST /feedback/{id}, NULL = belum diberi. Beda dari had_correction (sinyal implisit dari teks pesan berikutnya)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_routing_label ON routing_events(complexity_label, had_correction);

-- ===================== ROLE HANDOFFS [#4] =====================
CREATE TABLE IF NOT EXISTS role_handoffs (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
    from_role TEXT NOT NULL, to_role TEXT NOT NULL,
    task_input TEXT NOT NULL, contract_name TEXT NOT NULL,
    output_json TEXT, validation_ok INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ===================== APPROVAL LOG [audit gap] =====================
-- approval_id: kolom di skema BARU langsung (untuk DB baru); untuk DB LAMA
-- (dibuat sebelum kolom ini ada), _ensure_columns() menambalnya via ALTER TABLE
-- SETELAH executescript ini selesai. Index-nya (idx_approval_id) SENGAJA tidak
-- dibuat statis di sini — kalau CREATE INDEX dijalankan sebelum ALTER TABLE
-- menambal kolom di DB lama, "no such column: approval_id" (kolom belum ada
-- saat index dibuat). Index dibuat oleh DatabaseManager._ensure_columns()
-- setelah kolom dipastikan ada, aman untuk DB baru maupun lama.
CREATE TABLE IF NOT EXISTS approval_log (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL, tool_input TEXT,
    decision TEXT,                          -- pending | approved | rejected | timeout | auto:trust_mode | proposal:pending
    approval_id TEXT,                       -- [Human Approval Pipeline] kolom eksplisit — SEBELUMNYA hanya tersirat sebagai substring "pending:{id}" di decision, hilang setelah resolve. Query-able via GET /approval/{approval_id}
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ===================== APP SETTINGS (runtime override) =====================
-- Key-value sederhana untuk override yang bisa diubah lewat /settings tanpa restart.
-- mis. model_override_provider / model_override_model (paksa semua tier ke 1 model).
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ===================== CALIBRATION LOG [#1 self-calibration] =====================
-- Jejak audit setiap kali threshold router digeser dari rekomendasi kalibrasi.
-- Menutup loop Inovasi 1: audit → rekomendasi → APPLY (tercatat di sini) → revert.
-- Tiap baris menyimpan offset sebelum/sesudah + alasan + apakah masih aktif (untuk revert).
CREATE TABLE IF NOT EXISTS calibration_log (
    id INTEGER PRIMARY KEY,
    old_offset INTEGER NOT NULL,            -- threshold offset sebelum apply
    new_offset INTEGER NOT NULL,            -- threshold offset sesudah apply
    reason TEXT,                            -- ringkasan rekomendasi yang memicu (label/issue)
    source TEXT DEFAULT 'manual',           -- 'calibration' (dari saran) | 'revert' | 'manual'
    active INTEGER DEFAULT 1,               -- 1 = ini state aktif terakhir; 0 = sudah di-revert/digantikan
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_calibration_active ON calibration_log(active);

-- ===================== TOOL INVOCATIONS [telemetri tooling] =====================
-- Audit penggunaan tool: tool mana dipakai, role apa, hasil (ok/error/timeout),
-- latency. Setara Inovasi 1 untuk tools — menjawab "tool mana yang berguna".
-- Dicatat di titik eksekusi terpusat (_execute_tool), bukan per-tool.
CREATE TABLE IF NOT EXISTS tool_invocations (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    user_id TEXT DEFAULT 'default',        -- [Audit log format actor_is_agent, § Prioritas 2] lihat routing_events
    actor_is_agent INTEGER DEFAULT 1,
    tool_name TEXT NOT NULL,
    outcome TEXT NOT NULL,                  -- ok | error | timeout
    latency_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tool_invocations ON tool_invocations(tool_name, outcome);

-- ===================== CRYSTALLIZATION LOG [#3 observability] =====================
-- Jejak SETIAP percobaan kristalisasi (Inovasi 3), termasuk yang jadi draft/duplicate.
-- Tabel skills hanya menyimpan hasil yang tersimpan; ini membuat KEPUTUSAN evaluator
-- (confidence/gaps/active-vs-draft + model generator vs evaluator) kasat mata di /skills.
CREATE TABLE IF NOT EXISTS crystallization_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    generator_model TEXT,
    evaluator_model TEXT,
    confidence INTEGER,                     -- 1..5 dari self-evaluation
    critical_gaps INTEGER,                  -- 1 = ada gap kritis
    status TEXT NOT NULL,                   -- active | draft | duplicate
    reasoning TEXT,                         -- satu kalimat alasan evaluator
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_crystallization_role ON crystallization_log(role, status);

-- ===================== CONVERSATIONS [multi-agent persistence] =====================
-- Simpan transkrip percakapan multi-agent (pipeline/debate/orchestrator) agar bisa
-- ditinjau ulang. Ephemeral sebelumnya (in-memory) — hilang saat refresh. Satu baris
-- per run; transcript disimpan sebagai JSON list [[role, content], ...].
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    pattern TEXT NOT NULL,                  -- pipeline | debate | orchestrator
    participants TEXT,                      -- CSV peserta (lead-first utk orchestrator)
    initial_message TEXT,
    transcript_json TEXT NOT NULL,          -- JSON [[role, content], ...]
    turns INTEGER DEFAULT 0,
    end_reason TEXT,                        -- strategy_done | max_turns | stopped
    cost_usd REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);

-- ===================== AGENT TODOS [tool todo_write] =====================
-- Daftar langkah multi-step yang dikelola agent lewat tool todo_write, per sesi.
-- Satu baris = satu item; tiap panggilan todo_write MENGGANTI seluruh daftar sesi
-- (snapshot, pola sama harness). UI menampilkan progres agar user lihat rencana agent.
CREATE TABLE IF NOT EXISTS agent_todos (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    position INTEGER NOT NULL,               -- urutan dalam daftar
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | completed
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_todos_session ON agent_todos(session_id, position);

-- ===================== AGENT BLOCKERS [tool report_blocker] =====================
-- Terinspirasi "proactive blocker reporting" Multica: agent dapat MENANDAI hambatan
-- yang dihadapinya (mis. kredensial hilang, kebutuhan tak jelas) sebagai sinyal
-- terstruktur — bukan sekadar teks di jawaban. Ditampilkan menonjol di UI agar user
-- bisa menanggapi. Berbeda dari ask_user (yang MEMBLOKIR menunggu jawaban): blocker
-- bersifat asinkron — agent melaporkan lalu lanjut/berhenti, user meninjau kapan saja.
CREATE TABLE IF NOT EXISTS agent_blockers (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    summary TEXT NOT NULL,                   -- ringkas: apa yang menghambat
    detail TEXT,                             -- konteks tambahan (opsional)
    severity TEXT NOT NULL DEFAULT 'medium', -- low | medium | high
    status TEXT NOT NULL DEFAULT 'open',     -- open | resolved
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_blockers_status ON agent_blockers(status, created_at DESC);

-- ===================== AUTOPILOTS [scheduled agent runs] =====================
-- Terinspirasi "Autopilots" Multica: tugas berulang yang dijalankan otomatis (mis.
-- audit harian, ringkasan mingguan). KEAMANAN (CLAUDE.md §1, §17): autopilot berjalan
-- TANPA manusia di depan, jadi tool yang butuh approval TIDAK dieksekusi — melainkan
-- diantri sebagai PROPOSAL (approval_log pending) untuk ditinjau user. Scheduler =
-- loop asyncio in-process (tanpa dependency baru), interval sederhana (UTC, tanpa cron).
CREATE TABLE IF NOT EXISTS autopilots (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,                      -- role yang menjalankan tugas
    prompt TEXT NOT NULL,                    -- instruksi tugas terjadwal
    interval_sec INTEGER NOT NULL,           -- jeda antar-jalan (detik); UTC, tanpa cron
    enabled INTEGER NOT NULL DEFAULT 1,      -- 1=aktif, 0=jeda
    last_run_at TIMESTAMP,                   -- kapan terakhir dijalankan (NULL=belum)
    next_run_at TIMESTAMP,                   -- kapan due berikutnya (dihitung scheduler)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_autopilots_due ON autopilots(enabled, next_run_at);

-- Riwayat tiap eksekusi autopilot — agar user bisa meninjau hasil & proposal.
CREATE TABLE IF NOT EXISTS autopilot_runs (
    id INTEGER PRIMARY KEY,
    autopilot_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,                -- sesi run (tautkan ke routing_events dll)
    status TEXT NOT NULL DEFAULT 'running',  -- running | done | error
    output TEXT,                             -- ringkasan jawaban agent
    proposals INTEGER NOT NULL DEFAULT 0,    -- jumlah aksi destruktif yang DIANTRI (bukan dieksekusi)
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_autopilot_runs ON autopilot_runs(autopilot_id, id DESC);

-- ===================== SKILL CURATOR [I1: merge/dedup] =====================
-- Compounding intelligence: gabungkan skill mirip agar library tak terfragmentasi.
-- Anti kehilangan data (§1): loser TIDAK dihapus (status='merged', merged_into=winner);
-- semua revertible. Setiap merge tercatat di sini → kasat mata & dapat dibatalkan.
CREATE TABLE IF NOT EXISTS curation_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    action TEXT NOT NULL,                    -- merge | revert_merge
    status TEXT NOT NULL DEFAULT 'applied',  -- pending | applied | reverted (curation_auto=False → pending)
    winner_id INTEGER,                       -- skill yang bertahan / hasil sintesis
    loser_ids TEXT,                          -- JSON array id yang diserap
    similarity REAL,                         -- skor pre-filter leksikal (0..1)
    judge_confidence INTEGER,                -- 1..5 dari LLM judge
    merged_content TEXT,                     -- konten sintesis judge, disimpan sampai di-apply
    reasoning TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_curation_role ON curation_log(role, created_at DESC);

-- ===================== SKILL VERSIONS [I3: refine — revertible] =====================
-- Riwayat konten skill sebelum tiap refine/merge, agar perubahan dapat ditinjau & dibatalkan.
CREATE TABLE IF NOT EXISTS skill_versions (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id),
    version INTEGER NOT NULL,
    skill_content TEXT NOT NULL,
    reason TEXT,                             -- refine_on_correction | merge
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skill_versions(skill_id, version DESC);

-- ===================== SKILL USAGE PENDING [I2/I3: jembatan antar-turn] =====================
-- AgentLoop dibuat baru tiap request → "skill apa yang dipakai turn lalu" harus
-- dipersistenkan agar turn BERIKUTNYA (yang membawa sinyal had_correction) bisa
-- menentukan outcome: sukses → promote draft / revive; dikoreksi → reset draft + refine.
-- Satu baris per (session, turn): JSON daftar skill_id yang disuntik ke turn itu.
CREATE TABLE IF NOT EXISTS skill_usage_pending (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    skill_ids TEXT NOT NULL,                 -- JSON array skill_id yang dipakai turn ini
    resolved INTEGER NOT NULL DEFAULT 0,     -- 1 = sudah diproses outcome-nya
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skill_usage_pending ON skill_usage_pending(session_id, resolved, id DESC);

-- ===================== USER MODEL [I5: profil naratif lintas sesi] =====================
-- Opsional (user_model_enabled). Ringkasan naratif tentang user dari L2 facts + transkrip,
-- diperbarui throttled (1×/hari). Versioned + revertible (tak ada drift senyap). Lokal &
-- dapat dihapus user (privasi §1). Per role agar tiap peran punya lensa sendiri.
CREATE TABLE IF NOT EXISTS user_model (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    profile TEXT NOT NULL,                   -- ringkasan naratif singkat
    active INTEGER NOT NULL DEFAULT 1,        -- 1 = versi aktif disuntik ke context
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_model_role ON user_model(role, active, version DESC);

-- ===================== MCP SERVERS [tool eksternal via Model Context Protocol] =====================
-- Server MCP yang disambungkan agar agent memakai tool ekosistem MCP. KEAMANAN (§1):
-- server = kode pihak ketiga tak terkendali → tool yang ditemukan SELALU butuh approval
-- (HITL); remote (http) di-guard SSRF sebelum konek. Definisi disimpan di sini agar
-- dapat dikelola lewat /mcp tanpa edit kode. command/env/url disimpan sebagai teks/JSON.
CREATE TABLE IF NOT EXISTS mcp_servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,                -- nama unik (dipakai di prefix mcp__<name>__tool)
    transport TEXT NOT NULL,                  -- stdio | http
    command TEXT,                             -- stdio: argv sebagai JSON array
    url TEXT,                                 -- http: endpoint server MCP
    env TEXT,                                 -- stdio: env tambahan sebagai JSON object
    enabled INTEGER NOT NULL DEFAULT 1,       -- 1 = dimuat saat startup
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
