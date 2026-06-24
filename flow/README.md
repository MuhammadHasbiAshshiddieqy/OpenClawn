# 📐 Flow — Alur Eksekusi OpenCLAWN

Folder ini berisi dokumentasi **alur backend** dari setiap aksi penting di OpenCLAWN.
Setiap file menjelaskan **jalan cerita** dari satu flow: mulai dari HTTP request (atau pemicu),
fungsi apa yang dipanggil, data mengalir ke mana, tabel DB mana yang tersentuh, hingga response
kembali ke user.

---

## Daftar Flow

| # | Flow | File | Cerita |
|---|---|---|---|
| 1 | **Chat Single-Agent** | `chat-flow.md` | User kirim pesan → masuk shield → routing → LLM → tool loop → memory → response SSE. **Flow utama yang paling sering terjadi.** |
| 2 | **Skill Crystallization** | `skill-crystallization.md` | Bagaimana solusi agent berubah jadi **skill** yang bisa dipakai ulang. Inovasi 3: evaluasi mandiri, confidence threshold, status draft/active. |
| 3 | **Memory & Decay** | `memory-flow.md` | L0–L4 memory layers: checkpoint tiap turn, fakta penting, skill aktif, arsip lintas sesi. Plus **skill decay** (Inovasi 2) — skill jarang pakai memudar. |
| 4 | **Routing & Audit** | `routing-audit.md` | SmartRouter memilih model (Ollama lokal vs Gemini cloud) berdasarkan 8 dimensi query. Inovasi 1: setiap keputusan dicatat + dikoreksi user → kalibrasi otomatis. |
| 5 | **Tool Execution** | `tool-execution.md` | Tool loop iteratif: validasi input → approval → eksekusi di sandbox → truncate output. Mulai dari `file_read` sampai `code_run`. |
| 6 | **Multi-Agent Conversation** | `conversation-flow.md` | Beberapa role (PM → Dev → QA) saling ngobrol dalam 3 pola: pipeline, debate, orchestrator. Tiap giliran = AgentLoop.run() penuh. |
| 7 | **Security & Guardrails** | `security-flow.md` | Shield scan input, guardrails (NeMo-style), vault credential, approval gate HITL. Lapisan keamanan sebelum, selama, dan sesudah eksekusi. |
| 8 | **Autopilot & Calibration** | `autopilot-calibration.md` | Tugas terjadwal tanpa user, auto-apply kalibrasi router, curation skill duplikat. |

---

## Diagram Hubungan Antar Modul

```mermaid
flowchart TB
    subgraph Web["🌐 Web Layer (FastAPI)"]
        CHAT["/chat/stream"]
        CONV["/converse/stream"]
        METRICS["/metrics"]
        SETTINGS["/settings"]
        APPROVE["/approve"]
    end

    subgraph CORE["🧠 Core Layer"]
        AL["AgentLoop.run()"]
        LLM["LLMClient\nstream_with_fallback()"]
        ROUTER["SmartRouter\ndecide()"]
        AUDITOR["RoutingAuditor"]
        CRYSTAL["ConfidenceCrystallizer"]
        COMPACTOR["ContextCompactor"]
        CONV_ORCH["ConversationOrchestrator"]
        TOOL_LOOP["_run_tool_loop()"]
        TOOL_EXEC["_execute_tool()"]
        MCP["MCPClient"]
    end

    subgraph MEM["💾 Memory & Skills"]
        MM["MemoryManager\nload_context()"]
        DECAY["SkillDecayManager"]
        FEEDBACK["SkillFeedback"]
        CURATOR["SkillCuratorManager"]
        USER_MODEL["UserModel"]
    end

    subgraph SEC["🔒 Security"]
        SHIELD["Shield\nscan_input()"]
        GUARD["GuardrailEngine"]
        VAULT["Vault"]
        APPROVAL["ApprovalGate"]
        QUESTION["QuestionGate"]
    end

    subgraph DB[("🗄️ SQLite")]
        L1["memory_l1"]
        L2["memory_l2"]
        L3["skills"]
        L4["memory_l4 (FTS5)"]
        RE["routing_events"]
        CL["crystallization_log"]
        TI["tool_invocations"]
        CAL["calibration_log"]
        AS["app_settings"]
        CONV_TBL["conversations"]
        RH["role_handoffs"]
        AP["approval_log"]
        BL["agent_blockers"]
        TD["agent_todos"]
        AUTOP["autopilots"]
    end

    CHAT --> AL
    CONV --> CONV_ORCH
    CONV_ORCH --> AL
    AL --> SHIELD
    AL --> GUARD
    AL --> MM
    AL --> DECAY
    AL --> ROUTER
    ROUTER --> AUDITOR
    AL --> COMPACTOR
    AL --> TOOL_LOOP
    TOOL_LOOP --> TOOL_EXEC
    TOOL_LOOP --> LLM
    TOOL_EXEC --> VAULT
    TOOL_EXEC --> APPROVAL
    TOOL_EXEC --> TOOL_LOOP
    AL --> CRYSTAL
    AL --> FEEDBACK
    AL --> CURATOR

    MM --> L1
    MM --> L2
    MM --> L4
    DECAY --> L3
    FEEDBACK --> DECAY
    FEEDBACK --> CRYSTAL
    AUDITOR --> RE
    CRYSTAL --> CL
    CRYSTAL --> L3
    TOOL_EXEC --> TI
    AL --> CAL

    MCP -.->|external| MCP_SRV["MCP Servers"]
```

---

## Legenda Singkat

| Tabel | Isi | Ditulis oleh |
|---|---|---|
| `memory_l1` | Checkpoint tiap turn (state terakhir) | `AgentLoop._post_turn` |
| `memory_l2` | Fakta penting lintas sesi | `MemoryManager.add_fact` |
| `skills` | Skill active/draft/archived + decay score | `Crystallizer`, `SkillDecayManager` |
| `memory_l4` | Arsip sesi lama (FTS5, bisa di-search) | `MemoryManager.archive_session` |
| `routing_events` | Setiap keputusan router (8 dimensi + hasil) | `RoutingAuditor.log_decision/finalize` |
| `crystallization_log` | Jejak evaluasi skill (transparansi I3) | `Crystallizer._log_attempt` |
| `tool_invocations` | Telemetri tiap eksekusi tool | `AgentLoop._execute_tool` |
| `calibration_log` | Riwayat perubahan offset router | `CalibrationStore.apply/revert` |
| `conversations` | Transkrip multi-agent (JSON) | `ConversationOrchestrator._persist` |
| `approval_log` | Log approve/reject/proposal tool | `ApprovalGate` |
| `agent_todos` | To-do list item per sesi | `TodoWriteTool` |
| `agent_blockers` | Blocker yang dilaporkan agent | `ReportBlockerTool` |

---

> **Cara membaca:** Mulai dari `chat-flow.md` (flow termudah & paling sering). Setelah paham,
> lanjut ke `routing-audit.md` + `tool-execution.md` (komponen dalam chat). Lalu `memory-flow.md`
> + `skill-crystallization.md` (post-turn). Terakhir `conversation-flow.md` (multi-agent).
