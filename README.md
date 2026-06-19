<div align="center">
  <img src="assets/OpenClawn.png" alt="OpenCLAWN Logo" width="420" />

  <h1>OpenCLAWN</h1>
  <p><strong>Playful by Design. Powerful by Nature.</strong></p>
  <p>Lightweight, safe, self-improving multi-role agent framework</p>

  <p>
    <strong>Route Smarter</strong> · <strong>Learn Continuously</strong> · <strong>Stay Safe</strong> · <strong>Hand Off Cleanly</strong>
  </p>

  <p>
    <img src="https://github.com/MuhammadHasbiAshshiddieqy/OpenClawn/actions/workflows/ci.yml/badge.svg" alt="CI">
    <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python 3.12+">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
    <img src="https://img.shields.io/badge/LLM-Ollama%20%2B%20Gemini%20%2B%20Claude-purple" alt="Hybrid LLM">
    <img src="https://img.shields.io/badge/tools-18-orange" alt="18 Tools">
    <img src="https://img.shields.io/badge/tests-194%20passing-brightgreen" alt="Tests">
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

Plus a **multi-agent conversation layer** (pipeline / debate / orchestrator) where roles hand off
to each other, with live stop and interject.

**Stack:** Python 3.12 · FastAPI · HTMX · SQLite (aiosqlite) · Ollama + Gemini + Claude · httpx · Pydantic · structlog · tenacity

---

## Quick Start

```bash
git clone https://github.com/MuhammadHasbiAshshiddieqy/OpenClawn.git
cd OpenClawn

# Recommended: uv with the committed lockfile (reproducible, identical to CI)
uv sync --frozen --extra dev

# Or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Create .env from example
cp .env.example .env
# Fill in keys: GEMINI_API_KEY and/or ANTHROPIC_API_KEY (heavy tiers)
# Local-only is fine too — Ollama handles light tiers without any key

# Run database migration
mkdir -p data
sqlite3 data/openclawn.db < migrations/001_initial.sql

# Pull Ollama models — one per local tier (or just gemma4:e4b to start)
ollama pull gemma4:e2b
ollama pull gemma4:e4b
ollama pull gemma4:12b

# Build sandbox image for code_run / shell_run
docker build -t openclawn-sandbox:latest -f Dockerfile.sandbox .

# Start the app
uvicorn web.main:app --reload --port 8000
```

Open **http://localhost:8000** to chat · **http://localhost:8000/metrics** for the routing calibration dashboard.

---

## Architecture

### Component Overview

```mermaid
graph TB
    subgraph UI["Web UI — FastAPI + HTMX + SSE"]
        CHAT["/chat/stream · single agent"]
        CONVERSE["/converse/stream · multi-agent"]
        METRICS["/metrics · calibration dashboard"]
        SETTINGS["/settings · model override"]
    end

    subgraph CONVO["Multi-Agent Layer"]
        ORCH["ConversationOrchestrator"]
        PIPE["PipelineStrategy · PM &rarr; Dev &rarr; QA"]
        DEBATE["DebateStrategy · round-robin"]
        LEAD["OrchestratorStrategy · dynamic delegation"]
    end

    subgraph AGENT["Agent Loop — iterative, not recursive"]
        direction TB
        SHIELD["Shield · NFKD scan"]
        ROUTE["SmartRouter · soul-aware"]
        LLMCALL["LLM Client · stream + fallback"]
        TOOLLOOP["Tool Loop · max 5 hops + loop guard"]
        POST["Post-Turn · memory + decay + crystallize"]
    end

    subgraph MODULES["Core Modules"]
        AUDITOR["RoutingAuditor · innovation #1"]
        CALIB["RoutingCalibrator · tuning advisor"]
        MEMORY["MemoryManager · L1–L4 + FTS5"]
        DECAY["SkillDecay · innovation #2"]
        CRYSTAL["Crystallizer · innovation #3"]
        CONTRACTS["RoleNegotiator · innovation #4"]
        COMPACTOR["ContextCompactor · token budget"]
    end

    subgraph TOOLS["Tools — 18 total, all workspace-bounded"]
        FS["Filesystem · read/write/edit/append/patch/glob/grep/list_dir"]
        EXEC["Execution · code_run · shell_run (both sandboxed)"]
        NET["Network · web_fetch · web_search · http_request"]
        DATA["Data · db_query (SELECT) · memory_search · json_query · pdf_read"]
    end

    subgraph SECURITY["Security"]
        VAULT["Vault · API keys, never in context"]
        APPROVAL["ApprovalGate · human-in-the-loop"]
    end

    subgraph INFRA["Infrastructure"]
        DB["SQLite · aiosqlite · WAL · POWER()"]
        SANDBOX["Docker Sandbox · network none · read-only · non-root"]
    end

    UI --> CONVO
    UI --> AGENT
    CONVO --> AGENT
    AGENT --> MODULES
    AGENT --> TOOLS
    AGENT --> SECURITY
    AGENT --> INFRA
    EXEC --> SANDBOX
    MODULES --> DB
    SECURITY --> DB
```

### Full Agent Flow — One Turn

```mermaid
flowchart TD
    U(["User sends message via Web UI"]) --> SHIELD

    subgraph PRE["0 · Input Processing"]
        SHIELD{"Shield<br/>NFKD normalize<br/>+ danger pattern scan"}
        SHIELD -->|blocked| REJECT["Rejected"]
        SHIELD -->|clean| CORRECT
        CORRECT{"Check correction<br/>from previous turn?"}
        CORRECT -->|"yes: mark had_correction=1"| LOAD_SKILL
        CORRECT -->|no| LOAD_SKILL
    end

    subgraph MEM["1 · Memory Loading"]
        LOAD_SKILL["Load active skills<br/>SkillDecay: score &gt; 0.3,<br/>max 8, trigger-matched"]
        LOAD_SKILL --> LOAD_CTX["Load memory context<br/>L1: last state · L2: facts<br/>L3: active skills<br/>L4: FTS5 cross-session"]
    end

    subgraph BUILD["2 · Context Building"]
        LOAD_CTX --> COMPACT["ContextCompactor.build()<br/>system + memory + history<br/>+ message, within budget"]
    end

    subgraph ROUTE["3 · Routing Decision"]
        COMPACT --> DIMS["8 dimensions scored"]
        DIMS --> SOUL{"soul.toml<br/>upgrade_kw hit?"}
        SOUL -->|"yes: +3 score"| PREFER
        SOUL -->|no| PREFER
        PREFER{"prefer_local?"}
        PREFER -->|"yes: threshold +1<br/>stay local longer"| LABEL
        PREFER -->|"no: normal threshold"| LABEL
        LABEL["Complexity label<br/>TRIVIAL &rarr; SIMPLE &rarr; MODERATE<br/>&rarr; COMPLEX &rarr; CRITICAL"]
        LABEL --> OVERRIDE{"/settings<br/>override active?"}
        OVERRIDE -->|yes| USE_OV["Use chosen model<br/>(audit still logs router decision)"]
        OVERRIDE -->|no| USE_ROUTE["Use router model"]
    end

    subgraph AUDIT1["4 · Pre-Call Audit — innovation #1"]
        USE_OV --> LOG["Auditor.log_decision()<br/>8 dims + score + label<br/>+ model + reason &rarr; DB"]
        USE_ROUTE --> LOG
    end

    subgraph LLM["5 · LLM Call with Fallback"]
        LOG --> STREAM["LLMClient.stream_with_fallback()"]
        STREAM --> HEALTH{"Ollama health check"}
        HEALTH -->|up| PRIMARY["Try primary model"]
        HEALTH -->|down| FALL["Fallback chain"]
        PRIMARY -->|error| FALL
        FALL --> F1["1 · gemma4:e4b (local)"]
        F1 -->|error| F2["2 · deepseek-r1 (local)"]
        F2 -->|error| F3["3 · qwen3.5:9b (local)"]
        F3 -->|error| F4["4 · gemini-2.5-flash (cloud)"]
        F4 -->|error| FAIL["ProviderUnavailable"]
    end

    subgraph TOOL_LOOP["6 · Iterative Tool Loop — max 5 hops"]
        PRIMARY --> PARSE{"Tool call in stream?"}
        FALL --> PARSE
        PARSE -->|"tool_call found"| LOOPGUARD
        PARSE -->|"text only"| YIELD["Yield text to user"]
        YIELD --> DONE_CHECK{"Another tool call?"}
        DONE_CHECK -->|no| FINALIZE
        LOOPGUARD{"Same call<br/>repeated 2&times;?"}
        LOOPGUARD -->|yes| HALT["Loop halted<br/>(hard break)"]
        LOOPGUARD -->|no| ALLOWED
        HALT --> FINALIZE
        ALLOWED{"Role allowed?"}
        ALLOWED -->|no| ERR2["Tool denied"]
        ALLOWED -->|yes| APPROVAL{"requires_approval?"}
        APPROVAL -->|no| RUN_TOOL["Run tool"]
        APPROVAL -->|yes| HITL{"User approves?"}
        HITL -->|reject/timeout| ERR3["Approval denied"]
        HITL -->|approve| RUN_TOOL
        ERR2 --> TOOL_RESULT
        ERR3 --> TOOL_RESULT
        RUN_TOOL --> TOOL_RESULT["Result &rarr; append to messages"]
        TOOL_RESULT --> HOP{"hop &lt; 5?"}
        HOP -->|yes| PRIMARY
        HOP -->|no| FINALIZE
    end

    subgraph POST["7 · Post-Turn Processing"]
        FINALIZE["Auditor.finalize()<br/>tokens, cost, latency &rarr; DB"]
        FINALIZE --> WRITE_MEM["MemoryManager<br/>L1 checkpoint · L4 archive (if threshold)"]
        WRITE_MEM --> DECAY_PASS["SkillDecay.maybe_run_decay_pass()<br/>throttled: 1&times;/hour"]
        DECAY_PASS --> CRYST_CHECK{"Crystallizer<br/>should_attempt?<br/>(&ge;3 tool calls)"}
        CRYST_CHECK -->|yes| SELF_EVAL["Self-evaluate<br/>evaluator &ge; generator<br/>confidence 1–5"]
        SELF_EVAL --> STORE{"conf &ge; 4 AND<br/>no critical gaps?"}
        STORE -->|yes| ACTIVE["Store as active skill"]
        STORE -->|no| DRAFT["Store as draft<br/>(not auto-injected)"]
        CRYST_CHECK -->|no| DONE
        ACTIVE --> DONE
        DRAFT --> DONE
        DONE(["Turn complete"])
    end

    style REJECT fill:#f66,stroke:#900,color:#fff
    style FAIL fill:#f66,stroke:#900,color:#fff
    style HALT fill:#f66,stroke:#900,color:#fff
    style ACTIVE fill:#6f6,stroke:#090
    style DRAFT fill:#ff6,stroke:#990
    style DONE fill:#6cf,stroke:#069
```

### Tools & Security

All 25 tools are **workspace-bounded** — every file path is resolved with `Path.resolve()`
and rejected if it escapes the workspace root (defeats `../` and symlink escape). Tools that
mutate state or run code require explicit approval.

```mermaid
flowchart LR
    subgraph SAFE["No approval — read-only / inspect"]
        direction TB
        R1["file_read · list_dir · glob · grep"]
        R2["web_fetch · web_search · pdf_read"]
        R3["memory_search · json_query"]
    end

    subgraph GATED["Requires approval — mutate / execute / reach out"]
        direction TB
        G1["file_write · file_edit · file_append · apply_patch"]
        G2["code_run · shell_run"]
        G3["http_request · db_query (SELECT-only)"]
    end

    subgraph APPROVAL_GATE["ApprovalGate · HITL"]
        AG["Wait for user<br/>timeout: 120s<br/>fail-safe: deny"]
    end

    subgraph SANDBOX["Docker Sandbox — code_run AND shell_run"]
        direction TB
        S1["network none"]
        S2["read-only filesystem"]
        S3["non-root user"]
        S4["memory 256m · cpus 0.5"]
        S5["timeout 30s · no-new-privileges"]
    end

    GATED --> AG
    G2 --> SANDBOX
```

> **Security note:** `code_run` and `shell_run` **never execute on the host** — both run inside
> the Docker sandbox. If Docker is unavailable, they fail safe (return an error) rather than
> falling back to host execution. `db_query` is SELECT-only: write keywords and multi-statement
> SQL are rejected before the query reaches the database.

### The 4 Innovations — Where They Fire

```mermaid
flowchart LR
    subgraph TURN["One Agent Turn"]
        T1["Audit: log<br/>routing decision"] --> T2["Route: soul-aware<br/>8-dim scoring"]
        T2 --> T3["LLM call<br/>+ tool loop"]
        T3 --> T4["Audit: finalize<br/>tokens / cost / latency"]
        T4 --> T5["Decay pass<br/>(throttled)"]
        T5 --> T6["Crystallize<br/>(confidence-gated)"]
    end

    I1["#1 · Routing Audit<br/>+ Self-Calibration<br/><i>pre-call log + post-correct</i>"] -.-> T1
    I1 -.-> T4
    I2["#2 · Skill Decay<br/><i>exponential + throttle</i>"] -.-> T5
    I3["#3 · Confidence-Gated<br/>Crystallization<br/><i>eval &ge; generator</i>"] -.-> T6
    I4["#4 · Role Output<br/>Contracts<br/><i>Pydantic validated</i>"] -.-> T3
```

### Multi-Agent Conversation

Beyond single-agent turns, roles can **talk to each other**. One orchestrator loop drives three
pluggable strategies; each turn is a full agent run (routing, tools, memory all intact). You can
**stop** mid-conversation or **interject** with your own message, counted on the next turn.

```mermaid
flowchart TD
    START(["User message + mode"]) --> STRAT{"Strategy"}

    STRAT -->|Pipeline| P["PM &rarr; Dev &rarr; QA<br/>sequential, contract-validated handoff"]
    STRAT -->|Debate| D["Round-robin, N rounds<br/>full transcript shared each turn"]
    STRAT -->|Orchestrator| O["Lead delegates dynamically<br/>via JSON directive each turn"]

    O --> ODYN{"Directive<br/>parseable?"}
    ODYN -->|yes| OWORK["Route to chosen worker"]
    ODYN -->|no| OFALL["Fallback: lead &rarr; all workers &rarr; synthesis"]

    P --> NEXT{"next_speaker()"}
    D --> NEXT
    OWORK --> NEXT
    OFALL --> NEXT

    NEXT -->|role| RUN["Run AgentLoop for that role<br/>(cooperative stop check between tokens)"]
    RUN --> CONTRACT{"wants_contract?"}
    CONTRACT -->|yes, valid| REC["Record handoff · validation_ok=1"]
    CONTRACT -->|yes, invalid| DEG["Degrade: keep raw text<br/>validation_ok=0, continue"]
    CONTRACT -->|no| LOOP
    REC --> LOOP
    DEG --> LOOP
    LOOP{"stopped OR<br/>max_turns OR<br/>strategy done?"}
    LOOP -->|no| NEXT
    LOOP -->|yes| END(["conversation_end"])

    NEXT -->|none| END

    style END fill:#6cf,stroke:#069
    style DEG fill:#ff6,stroke:#990
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

The router scores 8 dimensions, then maps a complexity label to a model. Light tiers stay
**local** (Ollama, free, private); heavy tiers escalate to a **cloud** model. The exact mapping
is configurable. Local tiers are ordered **by model capacity** (harder case → more capable
model); heavy tiers go to the cloud. The shipped default:

```
Query complexity → model selection:

TRIVIAL  → gemma4:e4b          (Ollama · local, lightest)
SIMPLE   → deepseek-r1         (Ollama · local, reasoning)
MODERATE → qwen3.5:9b          (Ollama · local, most capable)
COMPLEX  → gemini-2.5-flash    (cloud)   # or claude-haiku-4-5
CRITICAL → gemini-2.5-pro      (cloud)   # or claude-sonnet-4-6
```

Cloud tiers are pluggable: point them at **Gemini** or **Claude** depending on the API key you
provide. The shipped default routes heavy tiers to Gemini; swap to Claude in `core/router.py` if
you prefer. Local tiers are easy to remap too — just edit the `MODELS` dict.

The router is **soul-aware**: each role's `soul.toml` can define `upgrade_keywords` that force
higher complexity, and `prefer_local=true` to resist escalating to the cloud. Soul upgrade
keywords **override** `prefer_local` — the soul has higher priority.

If Ollama is offline, the client falls back down the chain automatically
(`gemma4:e4b → deepseek-r1 → qwen3.5:9b → gemini-2.5-flash`). Every fallback is logged to the
audit DB.

---

## Project Structure

```
openclawn/
├── core/           # agent_loop · llm_client · router · audit · calibration
│                   # crystallizer · compactor · conversation (multi-agent)
├── infra/          # config · database (WAL, POWER()) · logging · env · workspace
├── memory/         # layers (L1–L4) · skill_decay · search (FTS5)
├── roles/          # pm/qa/dev soul.toml · contracts (Pydantic) · registry
├── tools/          # 25 tools: file_ops · read_many · search · shell · code · web · git
│                   # document (pdf_read · doc_write · pdf_write) · data · todo · interaction
├── security/       # vault · shield (NFKD) · approval (HITL gate)
├── web/            # FastAPI app · HTMX templates · SSE streaming
├── migrations/     # 001_initial.sql
└── tests/          # 17 files, 194 tests — one per innovation + tools + web
```

---

## Running Tests

```bash
pytest tests/ -v
```

All tests use in-memory SQLite and mocked LLM calls — no real Ollama, Gemini, or Claude API needed.

---

## Documentation

Detailed reference for every module, class, and function:

| Folder | Doc |
|---|---|
| `infra/` | [docs/infra.md](docs/infra.md) — config, database, logging |
| `core/` | [docs/core.md](docs/core.md) — agent loop, LLM client, router, audit, crystallizer, calibration, conversation |
| `memory/` | [docs/memory.md](docs/memory.md) — L1–L4 layers, skill decay, FTS5 search |
| `roles/` | [docs/roles.md](docs/roles.md) — contracts, role registry, soul.toml format |
| `security/` | [docs/security.md](docs/security.md) — vault, shield, approval gate HITL |
| `tools/` | [docs/tools.md](docs/tools.md) — 25 tools, permission matrix, Docker sandbox |
| `web/` | [docs/web.md](docs/web.md) — FastAPI endpoints, SSE streaming |
| Database | [docs/database.md](docs/database.md) — full schema + example queries |
| Tests | [docs/tests.md](docs/tests.md) — test index + patterns |

---

## Sprint Status

| Sprint | Focus | Status |
|---|---|---|
| 0 | Infra · LLM client · Agent loop · Web UI · Audit | Done |
| 1 | Soul-aware router · Memory L1–L4 · Compactor + caching | Done |
| 2 | Tools · Docker sandbox · Crystallizer · Skill decay | Done |
| 3 | Role contracts · Vault · Shield · ApprovalGate (HITL) | Done |
| 4 | Coverage · Calibration advisor · (router tuning needs live data) | Ongoing |
| 5 | Multi-agent conversation · 18-tool suite · Gemini provider · UI redesign | Done |

---

## Design Principles

- **Security first** — `code_run` and `shell_run` only run inside Docker (`network none`, `read-only`, non-root, timeout); they never touch the host
- **Workspace-bounded** — every file tool resolves paths and rejects escapes outside the workspace root
- **No SDK** — raw `httpx` for all LLM calls, intentional for audit transparency
- **Token-first** — context budget tracked; prompt caching on stable system blocks
- **No hardcoded domain/locale** — locale via field, not in code
- **Every innovation = extractable module** — `skill_decay`, `audit`, `crystallizer`, `contracts` have clean interfaces

---

## License

MIT — see [LICENSE](LICENSE)
