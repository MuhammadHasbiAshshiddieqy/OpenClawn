<div align="center">
  <img src="assets/OpenClawn.png" alt="OpenCLAWN Logo" width="420" />

  <h1>OpenCLAWN</h1>
  <p><strong>Playful by Design. Powerful by Nature.</strong></p>
  <p>Lightweight, safe, self-improving multi-role agent framework</p>

  <p>
    <strong>Route Smarter</strong> · <strong>Learn Continuously</strong> · <strong>Stay Safe</strong> · <strong>Hand Off Cleanly</strong>
  </p>

  <p>
    <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python 3.12+">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
    <img src="https://img.shields.io/badge/LLM-Ollama%20%2B%20Claude-purple" alt="Hybrid LLM">
    <img src="https://img.shields.io/badge/tests-135%20passing-brightgreen" alt="Tests">
  </p>
</div>

---

## What is OpenCLAWN?

OpenCLAWN is an agent framework built around **4 core innovations** that most agent frameworks skip:

| Innovation | Problem Solved |
|---|---|
| **Routing audit + self-calibration** | No agent records *why* a routing decision was made or whether it was correct |
| **Skill decay** | Skill trees accumulate forever — stale skills pollute context |
| **Confidence-gated crystallization** | Self-evolving agents store skills from bad solutions |
| **Role output contracts** | Multi-agent handoffs without typed contracts are fragile |

**Stack:** Python 3.12 · FastAPI · HTMX · SQLite (aiosqlite) · Ollama + Claude API · httpx · Pydantic · structlog · tenacity

---

## Quick Start

```bash
git clone https://github.com/MuhammadHasbiAshshiddieqy/OpenClawn.git
cd OpenClawn

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Create .env from example
cp .env.example .env
# Fill in ANTHROPIC_API_KEY in .env

# Run database migration
mkdir -p data
sqlite3 data/openclawn.db < migrations/001_initial.sql

# Pull Ollama models
ollama pull gemma4:e2b
ollama pull gemma4:e4b
ollama pull gemma4:12b

# Build sandbox image for code_run
docker build -t openclawn-sandbox:latest -f Dockerfile.sandbox .

# Start the app
uvicorn web.main:app --reload --port 8000
```

Open **http://localhost:8000** to chat · **http://localhost:8000/metrics** for the routing calibration dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              WEB UI (HTMX + SSE)             │
│         chat · /metrics dashboard            │
└──────────────────────┬──────────────────────┘
                       │
┌──────────────────────▼──────────────────────┐
│                  AGENT LOOP                  │
│  perceive → route → LLM → tool → memory     │
│  (iterative tool loop, not recursive)        │
└───┬──────────┬───────────┬──────────────────┘
  │          │           │
┌───▼────┐ ┌───▼────┐ ┌───▼──────────────────┐
│ ROUTER │ │ MEMORY │ │ SKILLS + CRYSTALLIZER │
│soul-   │ │ L1-L4  │ │ decay · confidence    │
│aware   │ │ FTS5   │ │ gating · evaluation   │
│[#1]    │ └────────┘ └──────────────────────┘
└───┬────┘
  │
┌───▼────┐   ┌────────────────┐   ┌───────────┐
│ AUDIT  │   │ ROLE CONTRACTS │   │ APPROVAL  │
│+CALIB  │   │ PM · QA · Dev  │   │   GATE    │
│[#1]    │   │    [#4]        │   │  (HITL)   │
└────────┘   └────────────────┘   └───────────┘
┌─────────────────────────────────────────────┐
│  LLM CLIENT: retry + backoff + fallback      │
│  Ollama (local) ↔ Claude API · caching       │
└─────────────────────────────────────────────┘
┌─────────────────────────────────────────────┐
│  SANDBOX: code_run in Docker (no-net, ro)    │
└─────────────────────────────────────────────┘
```

---

## The 4 Core Innovations

### 1. Routing Audit + Self-Calibration
Every routing decision is logged **before** the LLM call with 8 dimensions (token count, tech keywords, soul upgrade hits, etc.) and updated **after** with latency, cost, and correction signals. The `/metrics` dashboard shows which complexity labels have the highest correction rate — letting you tune the router with real data.

### 2. Skill Decay
Skills age with **exponential decay** (`score × 0.97^days_since_used`). Unused skills drop below 0.3 and get archived. A revived skill recovers score immediately. Decay runs throttled (max once per hour) so it never blocks a turn.

### 3. Confidence-Gated Crystallization
After a successful multi-step task, the agent evaluates its own solution using a model **at least as capable as the generator** (`EVALUATOR_FOR` map: e4b→12b, Sonnet→Sonnet). Solutions with confidence < 4/5 or critical gaps are stored as `draft`, not `active`, and never injected into future context automatically.

### 4. Role Output Contracts
Handoffs between roles (PM → QA → Dev) use Pydantic models as typed contracts. Invalid output is stored with `validation_ok=0` for debugging — no crash, no silent data loss.

---

## LLM Routing

```
Query complexity → model selection:

TRIVIAL  → gemma4:e2b  (Ollama, local)
SIMPLE   → gemma4:e4b  (Ollama, local)
MODERATE → gemma4:12b  (Ollama, local)
COMPLEX  → claude-haiku-4-5-20251001  (Anthropic API)
CRITICAL → claude-sonnet-4-6          (Anthropic API)
```

The router is **soul-aware**: each role's `soul.toml` can define `upgrade_keywords` that force higher complexity, and `prefer_local=true` to resist escalating to the cloud. Soul upgrade keywords **override** `prefer_local` — the soul has higher priority.

If Ollama is offline, the client falls back down the chain automatically. Every fallback is logged to the audit DB.

---

## Project Structure

```
openclawn/
├── core/           # agent_loop · llm_client · router · audit · crystallizer · compactor
├── infra/          # config · database (WAL, POWER()) · logging (structlog JSON)
├── memory/         # layers (L1-L4) · skill_decay · search (FTS5)
├── roles/          # pm/qa/dev soul.toml · contracts (Pydantic) · registry
├── tools/          # file_read · file_write · web_fetch · code_run · ask_user
├── security/       # vault · shield (NFKD) · approval_gate
├── web/            # FastAPI app · HTMX templates · SSE streaming
├── migrations/     # 001_initial.sql
└── tests/          # test_router · test_fallback · test_skill_decay
                    # test_crystallizer · test_contracts
```

---

## Running Tests

```bash
pytest tests/ -v
```

All tests use in-memory SQLite and mocked LLM calls — no real Ollama or Claude API needed.

---

## Documentation

Detailed reference for every module, class, and function:

| Folder | Doc |
|---|---|
| `infra/` | [docs/infra.md](docs/infra.md) — config, database, logging |
| `core/` | [docs/core.md](docs/core.md) — agent loop, LLM client, router, audit, crystallizer, calibration |
| `memory/` | [docs/memory.md](docs/memory.md) — L1–L4 layers, skill decay, FTS5 search |
| `roles/` | [docs/roles.md](docs/roles.md) — contracts, role registry, soul.toml format |
| `security/` | [docs/security.md](docs/security.md) — vault, shield, approval gate HITL |
| `tools/` | [docs/tools.md](docs/tools.md) — file/web/code tools, Docker sandbox |
| `web/` | [docs/web.md](docs/web.md) — FastAPI endpoints, SSE streaming |
| Database | [docs/database.md](docs/database.md) — full schema + example queries |
| Tests | [docs/tests.md](docs/tests.md) — test index + patterns |

---

## Sprint Status

| Sprint | Focus | Status |
|---|---|---|
| 0 | Infra · LLM client · Agent loop · Web UI · Audit | ✅ Done |
| 1 | Soul-aware router · Memory L1-L4 · Compactor + caching | ✅ Done |
| 2 | Tools · Docker sandbox · Crystallizer · Skill decay | ✅ Done |
| 3 | Role contracts · Vault · Shield · ApprovalGate (HITL) | ✅ Done |
| 4 | Coverage · Calibration advisor · (router tuning needs live data) | 🔶 Ongoing |

---

## Design Principles

- **Security first** — `code_run` only runs inside Docker (`--network none`, `--read-only`, non-root, timeout)
- **No SDK** — raw `httpx` for all LLM calls, intentional for audit transparency
- **Token-first** — context budget tracked; prompt caching on stable system blocks
- **No hardcoded domain/locale** — locale via field, not in code
- **Every innovation = extractable module** — `skill_decay`, `audit`, `crystallizer`, `contracts` have clean interfaces

---

## License

MIT — see [LICENSE](LICENSE)
