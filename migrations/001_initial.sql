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

-- ===================== SKILLS + DECAY [#2] =====================
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL,
    skill_name TEXT NOT NULL, trigger_pattern TEXT, skill_content TEXT NOT NULL,
    visibility TEXT DEFAULT 'private',     -- private | shared | inherited
    status TEXT DEFAULT 'active',          -- active | draft | archived
    confidence REAL DEFAULT 0.0,
    generator_model TEXT,                  -- model yang menghasilkan [#3, untuk evaluator gating]
    use_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    decay_score REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
CREATE TABLE IF NOT EXISTS approval_log (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL, tool_input TEXT,
    decision TEXT,                          -- approved | rejected | timeout
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
