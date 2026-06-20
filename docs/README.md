# Dokumentasi OpenCLAWN

Referensi teknis lengkap untuk semua modul dan fungsi dalam OpenCLAWN. Dibaca bersama [`CLAUDE.md`](../CLAUDE.md) dan [`openclawn-core-spec-v0.3.md`](../openclawn-core-spec-v0.3.md).

---

## Panduan Cepat

| Saya ingin tahu tentang... | Baca |
|---|---|
| Konfigurasi global, koneksi DB, logging, **SettingsStore** | [infra.md](infra.md) |
| Agent loop, LLM client (Ollama/Claude/**Gemini**), router (**multibahasa**), audit, crystallizer | [core.md](core.md) |
| **Multi-agent conversation** (pipeline/debate/orchestrator) | [core.md](core.md) · [web.md](web.md) |
| **Autopilots** (tugas terjadwal, proposal-gated) · **Activity timeline** | [core.md](core.md) · [web.md](web.md) |
| **Skill packs** (export/import antar-instalasi) | [core.md](core.md) · [web.md](web.md) |
| **MCP** — tool dari server MCP eksternal (stdio/HTTP, approval-gated) | [core.md](core.md) · [tools.md](tools.md) · [web.md](web.md) |
| Sistem memori L1–L4, skill decay, FTS5 search | [memory.md](memory.md) |
| **Compounding**: curator (merge), draft promotion, refine, user model | [memory.md](memory.md) · [core.md](core.md) |
| Role PM/QA/Dev/Data/Security, handoff contract, soul.toml | [roles.md](roles.md) |
| Vault, Shield (+**SSRF guard**), ApprovalGate HITL (+proposal queue) | [security.md](security.md) · [tools.md](tools.md) |
| Tool file/web/code/**blocker**, Docker sandbox | [tools.md](tools.md) |
| Endpoint FastAPI, SSE streaming, Web UI, **/settings** | [web.md](web.md) |
| Pilih/override model (Gemini, dll.) | [infra.md](infra.md) · [web.md](web.md) |
| Schema tabel SQLite, query contoh | [database.md](database.md) |
| Cara menulis test, daftar test per file | [tests.md](tests.md) |
| Script seed routing & sensitivity analysis | [scripts.md](scripts.md) |

---

## Peta Arsitektur → Dokumen

```
web/main.py          → web.md
│
├── AgentLoop        → core.md (agent_loop)
│   ├── LLMClient    → core.md (llm_client)
│   ├── SmartRouter  → core.md (router — multibahasa)
│   ├── RoutingAuditor / CalibrationStore → core.md (audit, calibration)
│   ├── ContextCompactor → core.md (compactor)
│   ├── ConfidenceCrystallizer → core.md (crystallizer + refine I3)
│   ├── MemoryManager → memory.md (layers)
│   ├── SkillDecayManager → memory.md (skill_decay + draft promotion I2)
│   ├── SkillFeedback / SkillCuratorManager / UserModel → memory.md (I1/I2/I3/I5)
│   ├── ApprovalGate → security.md (HITL + queue_proposal)
│   ├── Shield / Vault → security.md
│   └── tool_audit · activity · autopilot · skill_pack → core.md
│
├── ConversationOrchestrator → core.md (conversation: pipeline/debate/orchestrator)
├── AutopilotScheduler → core.md (autopilot — scheduled, proposal-gated)
│
├── TOOL_REGISTRY (26 tools)
│   ├── file_ops · read_many · search · shell · code (+ DockerSandbox) → tools.md
│   ├── web (web_fetch/web_search/http_request + SSRF guard) → tools.md
│   ├── git · document (pdf/doc) · data · todo · report_blocker → tools.md
│   └── ask_user (QuestionGate) → tools.md
│
└── RoleNegotiator   → roles.md (registry + contracts)
    └── PM / QA / Dev / Data / Security Output
```

---

## 4 Inovasi Inti — Letak di Dokumentasi

| Inovasi | File | Dokumen |
|---|---|---|
| **1. Routing Audit + Self-Calibration** | `core/audit.py`, `core/calibration.py`, `scripts/` | [core.md §audit](core.md), [core.md §calibration](core.md), [scripts.md](scripts.md) |
| **2. Skill Decay** | `memory/skill_decay.py` | [memory.md §skill_decay](memory.md) |
| **3. Confidence-Gated Crystallization** | `core/crystallizer.py` | [core.md §crystallizer](core.md) |
| **4. Role Output Contracts** | `roles/contracts.py`, `roles/registry.py` | [roles.md](roles.md) |

---

## Cara Update Dokumentasi Ini

Dokumentasi ini dihasilkan dari membaca source code secara langsung. Saat ada perubahan kode:

1. **Fungsi berubah signature/behavior** → update file `docs/*.md` yang relevan
2. **File/kelas baru ditambahkan** → tambahkan section baru di dokumen folder yang sesuai
3. **Tabel DB berubah** → update [database.md](database.md)
4. **Test baru ditambahkan** → update [tests.md](tests.md) dengan deskripsi singkat

Aturan: dokumentasi harus selalu mencerminkan **kode yang ada sekarang**, bukan rencana atau state masa lalu.
