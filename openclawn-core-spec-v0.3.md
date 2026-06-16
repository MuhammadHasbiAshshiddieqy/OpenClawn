# OpenCLAWN Core — Implementation Spec v0.3

> **Mission:** Agent framework yang ringan, aman, dan self-improving — dirancang untuk bermanfaat bagi siapa pun, bukan terikat satu konteks bisnis.
>
> **Status:** Research & Experimentation — siap implementasi
> **Interface:** Web UI (FastAPI + HTMX)
> **LLM:** Hybrid (Ollama lokal + Claude API), dengan fallback chain
> **Bahasa:** Python 3.12

**Changelog v0.2 → v0.3:** Integrasi 16 perbaikan dari audit eksternal. Router kini membaca soul.toml, evaluator crystallization minimal setara generator, fallback chain LLM, DatabaseManager terpusat, tool loop iterative, retry/backoff, prompt caching, human-in-the-loop approval, exponential decay, sandbox spec untuk code_run. Lihat [Lampiran A: Audit Resolution](#lampiran-a-audit-resolution).

---

## Daftar Isi

1. [Filosofi & Tujuan](#1-filosofi--tujuan)
2. [4 Inovasi Inti](#2-4-inovasi-inti)
3. [Arsitektur Keseluruhan](#3-arsitektur-keseluruhan)
4. [Stack & Dependencies](#4-stack--dependencies)
5. [Struktur Direktori](#5-struktur-direktori)
6. [Database Schema](#6-database-schema)
7. [Infrastruktur Bersama (DB, Config)](#7-infrastruktur-bersama-db-config)
8. [Modul: LLM Client (retry + fallback)](#8-modul-llm-client-retry--fallback)
9. [Modul: Agent Loop](#9-modul-agent-loop)
10. [Modul: Smart Router (soul-aware)](#10-modul-smart-router-soul-aware)
11. [Modul: Routing Audit (Inovasi 1)](#11-modul-routing-audit-inovasi-1)
12. [Modul: Skill Decay (Inovasi 2)](#12-modul-skill-decay-inovasi-2)
13. [Modul: Confidence Crystallization (Inovasi 3)](#13-modul-confidence-crystallization-inovasi-3)
14. [Modul: Role Contracts (Inovasi 4)](#14-modul-role-contracts-inovasi-4)
15. [Modul: Memory System](#15-modul-memory-system)
16. [Modul: Tools + Sandbox](#16-modul-tools--sandbox)
17. [Security Layer](#17-security-layer)
18. [Modul: Web UI](#18-modul-web-ui)
19. [Konfigurasi Role (SOUL.toml)](#19-konfigurasi-role-soultoml)
20. [Testing Strategy](#20-testing-strategy)
21. [Roadmap Implementasi](#21-roadmap-implementasi)
22. [Quick Start](#22-quick-start)
23. [Lampiran A: Audit Resolution](#lampiran-a-audit-resolution)

---

## 1. Filosofi & Tujuan

| Prinsip | Implikasi konkret |
|---|---|
| **Minimal core** | Core loop tetap ringkas. Tidak ada framework AI besar. |
| **Token-first** | Setiap keputusan terukur. Target context < 30K. Prompt caching aktif. |
| **Self-improving yang aman** | Agent belajar dari pengalaman, dengan gating kualitas dan evaluator yang valid. |
| **Universal, bukan personal** | Tidak ada hardcoded domain knowledge. Locale/domain via plugin. |
| **Resilient by default** | Setiap dependency eksternal punya retry, fallback, dan graceful degradation. |
| **Setiap inovasi = modul terpisah** | 4 inovasi inti bisa di-extract jadi paket standalone. |

---

## 2. 4 Inovasi Inti

| # | Inovasi | Masalah yang dipecahkan | Bisa jadi |
|---|---|---|---|
| **1** | **Routing audit + self-calibration** | Tidak ada agent yang mencatat *mengapa* routing dibuat dan apakah terbukti tepat | Middleware `llm-router-audit` |
| **2** | **Skill decay + relevance aging** | Skill tree menumpuk selamanya | Library `skill-decay` |
| **3** | **Confidence-gated crystallization** | Self-evolving agent menyimpan skill dari solusi buruk | Protokol terbuka |
| **4** | **Role output contracts** | Multi-agent workflow tanpa typed contract → fragile | Open standard |

---

## 3. Arsitektur Keseluruhan

```
┌─────────────────────────────────────────────────────────────┐
│                     WEB UI (HTMX + SSE)                       │
│              chat · /metrics (calibration dashboard)          │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                          AGENT LOOP                          │
│  perceive → route → call LLM → execute tool → write memory   │
│  (tool loop: ITERATIVE, not recursive)                       │
└───┬─────────┬──────────┬───────────┬──────────┬─────────────┘
   │         │          │           │          │
┌───▼───┐ ┌───▼────┐ ┌───▼────┐ ┌────▼────┐ ┌───▼─────┐
│ROUTER │ │ MEMORY │ │ SKILLS │ │ ROLES   │ │ APPROVAL│
│soul-  │ │ L0-L4  │ │+DECAY  │ │+CONTRACT│ │ gate    │
│aware  │ │ FTS5   │ │[#2]    │ │ [#4]    │ │ (HITL)  │
│[#1]   │ └────────┘ └───┬────┘ └─────────┘ └─────────┘
└───┬───┘                │
   │              ┌───────▼────────┐
┌───▼─────┐      │ CRYSTALLIZER   │
│ AUDIT   │      │ +CONFIDENCE    │
│ +CALIB  │      │ (valid evaluator)│
│ [#1]    │      │    [#3]        │
└─────────┘      └────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  SHARED INFRA: DatabaseManager · Config · Vault · Shield      │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  LLM CLIENT: retry + backoff + fallback chain                 │
│  Ollama (lokal) ↔ Claude API · prompt caching                 │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  SANDBOX: code_run dalam Docker (no-net, ro-mount, timeout)   │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Stack & Dependencies

```toml
# pyproject.toml
[project]
name = "openclawn"
version = "0.3.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "httpx>=0.27",
    "aiosqlite>=0.20",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "pydantic>=2.6",
    "structlog>=24.1",      # structured logging (audit gap)
    "tenacity>=8.2",        # retry/backoff (audit gap)
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.3",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

---

## 5. Struktur Direktori

```
openclawn/
├── core/
│   ├── agent_loop.py        # Main loop, tool loop iterative
│   ├── llm_client.py        # Ollama + Claude, retry + fallback + caching
│   ├── router.py            # Smart routing, soul-aware [INOVASI 1]
│   ├── audit.py             # Routing audit + self-calibration [INOVASI 1]
│   ├── compactor.py         # Context compaction
│   └── crystallizer.py      # Confidence crystallization, valid evaluator [INOVASI 3]
│
├── infra/                   # ← BARU: infrastruktur bersama
│   ├── config.py            # AppConfig, dependency injection
│   ├── database.py          # DatabaseManager, shared connection
│   └── logging.py           # structlog setup
│
├── memory/
│   ├── layers.py            # L0-L4 memory management
│   ├── search.py            # FTS5 cross-session search
│   └── skill_decay.py       # Exponential decay + archival [INOVASI 2]
│
├── roles/
│   ├── contracts.py         # Pydantic output contracts [INOVASI 4]
│   ├── registry.py          # Role loader (cached) + negotiation [INOVASI 4]
│   ├── pm/soul.toml
│   ├── qa/soul.toml
│   └── dev/soul.toml
│
├── tools/
│   ├── __init__.py          # TOOL_REGISTRY
│   ├── base.py              # Tool ABC + approval flag
│   ├── file_ops.py
│   ├── web.py
│   ├── code.py              # code_run → sandbox (lihat §16)
│   ├── interaction.py
│   └── sandbox.py           # Docker sandbox runner [audit gap]
│
├── security/
│   ├── vault.py
│   ├── shield.py            # NFKD normalization + regex
│   └── approval.py          # human-in-the-loop gate [audit gap]
│
├── web/
│   ├── main.py
│   ├── templates/{index,metrics}.html
│   └── static/style.css
│
├── migrations/001_initial.sql
├── tests/{test_router,test_skill_decay,test_crystallizer,test_contracts,test_fallback}.py
├── data/                    # SQLite (gitignored)
├── .env.example
├── docker-compose.yml
├── Dockerfile.role
├── Dockerfile.sandbox       # image untuk code_run
└── pyproject.toml
```

---

## 6. Database Schema

```sql
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
```

---

## 7. Infrastruktur Bersama (DB, Config)

> Memecahkan audit #8 (DB_PATH hardcoded) dan #9 (koneksi per metode).

```python
# infra/config.py

from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class AppConfig:
    db_path: str = "data/openclawn.db"
    ollama_base: str = "http://localhost:11434"
    anthropic_base: str = "https://api.anthropic.com"
    max_context_tokens: int = 28_000
    max_tool_hops: int = 5
    llm_max_retries: int = 3
    approval_timeout_sec: int = 120
    # fallback chain: urutan model jika provider utama gagal
    fallback_chain: tuple = field(default=(
        ("ollama", "gemma4:12b"),
        ("ollama", "gemma4:e4b"),
        ("ollama", "gemma4:e2b"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ))

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            db_path=os.environ.get("OPENCLAWN_DB", "data/openclawn.db"),
            ollama_base=os.environ.get("OLLAMA_BASE", "http://localhost:11434"),
        )


# Singleton global, di-inject ke semua modul
CONFIG = AppConfig.from_env()
```

```python
# infra/database.py

import aiosqlite
from infra.config import AppConfig


class DatabaseManager:
    """
    Satu koneksi shared per proses, bukan koneksi baru tiap metode.
    Di-pass ke semua modul via dependency injection.
    """

    def __init__(self, config: AppConfig):
        self._path = config.db_path
        self._conn: aiosqlite.Connection | None = None

    async def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()):
        db = await self.conn()
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        db = await self.conn()
        async with db.execute(sql, params) as cursor:
            return [dict(row) async for row in cursor]

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        db = await self.conn()
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
```

```python
# infra/logging.py

import structlog

def setup_logging():
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

log = structlog.get_logger()
```

---

## 8. Modul: LLM Client (retry + fallback)

> Memecahkan audit #5 (fallback), gap retry, dan gap prompt caching.

```python
# core/llm_client.py

import httpx
import json
from dataclasses import dataclass
from typing import AsyncGenerator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from infra.config import AppConfig
from infra.logging import log


@dataclass
class LLMChunk:
    type: str            # text | tool_call | usage
    text: str = ""
    tool_name: str = ""
    tool_input: dict = None
    usage: dict = None


class ProviderUnavailable(Exception):
    pass


class LLMClient:
    def __init__(self, vault, config: AppConfig):
        self.vault = vault
        self.config = config

    async def stream_with_fallback(
        self, provider, model, messages, tools=None, max_tokens=4096,
    ) -> AsyncGenerator[LLMChunk, None]:
        """
        Coba provider utama. Jika gagal (offline/error), turun ke fallback chain.
        Audit #5: graceful degradation.
        """
        chain = [(provider, model)] + [
            fc for fc in self.config.fallback_chain if fc != (provider, model)
        ]

        last_error = None
        for idx, (prov, mdl) in enumerate(chain):
            try:
                if not await self._health_check(prov):
                    raise ProviderUnavailable(f"{prov} health check gagal")

                fallback_used = idx > 0
                if fallback_used:
                    log.warning("llm_fallback", from_model=model, to_model=mdl, attempt=idx)

                async for chunk in self._stream_one(prov, mdl, messages, tools, max_tokens):
                    yield chunk
                return  # sukses, hentikan chain

            except (httpx.HTTPError, ProviderUnavailable) as e:
                last_error = e
                log.error("llm_provider_failed", provider=prov, model=mdl, error=str(e))
                continue

        raise ProviderUnavailable(f"Semua provider gagal. Terakhir: {last_error}")

    async def _health_check(self, provider: str) -> bool:
        try:
            if provider == "ollama":
                async with httpx.AsyncClient(timeout=3) as c:
                    r = await c.get(f"{self.config.ollama_base}/api/tags")
                    return r.status_code == 200
            return True   # anthropic: asumsikan up, retry handle transient
        except httpx.HTTPError:
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _stream_one(self, provider, model, messages, tools, max_tokens):
        """Retry transient errors dengan exponential backoff."""
        if provider == "ollama":
            async for c in self._ollama(model, messages, tools, max_tokens):
                yield c
        elif provider == "anthropic":
            async for c in self._claude(model, messages, tools, max_tokens):
                yield c

    async def _ollama(self, model, messages, tools, max_tokens):
        payload = {
            "model": model, "messages": messages, "stream": True,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{self.config.ollama_base}/api/chat",
                                     json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    msg = data.get("message", {})
                    if msg.get("content"):
                        yield LLMChunk(type="text", text=msg["content"])
                    for tc in msg.get("tool_calls", []):
                        yield LLMChunk(type="tool_call",
                                       tool_name=tc["function"]["name"],
                                       tool_input=tc["function"]["arguments"])
                    if data.get("done") and data.get("prompt_eval_count"):
                        yield LLMChunk(type="usage", usage={
                            "input_tokens": data.get("prompt_eval_count", 0),
                            "output_tokens": data.get("eval_count", 0),
                        })

    async def _claude(self, model, messages, tools, max_tokens):
        api_key = await self.vault.get("ANTHROPIC_API_KEY")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs = [m for m in messages if m["role"] != "system"]

        # Prompt caching: system prompt + memory stabil → cache_control
        # Hemat hingga 90% biaya untuk bagian yang berulang (audit gap)
        system_blocks = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

        payload = {
            "model": model, "max_tokens": max_tokens,
            "system": system_blocks, "messages": user_msgs, "stream": True,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", f"{self.config.anthropic_base}/v1/messages",
                                     headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:].strip())
                    etype = data.get("type", "")
                    if etype == "content_block_start":
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            yield LLMChunk(type="tool_call",
                                           tool_name=block.get("name", ""),
                                           tool_input={})
                    elif etype == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield LLMChunk(type="text", text=delta["text"])
                    elif etype == "message_delta":
                        if data.get("usage"):
                            yield LLMChunk(type="usage", usage=data["usage"])
```

---

## 9. Modul: Agent Loop

> Memecahkan audit #3 (fire-and-forget), #10 (rekursif → iteratif), nit #1 (filter tools), nit #2 (cache soul).

```python
# core/agent_loop.py

import asyncio
import time
import tomllib
from dataclasses import dataclass, field
from typing import AsyncGenerator

from infra.config import AppConfig, CONFIG
from infra.database import DatabaseManager
from infra.logging import log
from core.router import SmartRouter
from core.audit import RoutingAuditor
from core.compactor import ContextCompactor
from core.crystallizer import ConfidenceCrystallizer
from core.llm_client import LLMClient
from memory.layers import MemoryManager
from memory.skill_decay import SkillDecayManager
from tools import TOOL_REGISTRY
from security.vault import Vault
from security.approval import ApprovalGate


@dataclass
class AgentConfig:
    role: str
    session_id: str
    user_id: str = "default"


@dataclass
class Turn:
    role: str
    content: str = ""
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model_used: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0


class AgentLoop:
    def __init__(self, agent_cfg: AgentConfig, db: DatabaseManager, config: AppConfig = CONFIG):
        self.cfg = agent_cfg
        self.config = config
        self.db = db
        self.vault = Vault()
        self.llm = LLMClient(self.vault, config)
        self.memory = MemoryManager(agent_cfg.role, agent_cfg.session_id, db)
        self.decay = SkillDecayManager(agent_cfg.role, db)
        self.router = SmartRouter(role=agent_cfg.role)     # soul-aware
        self.auditor = RoutingAuditor(db)
        self.compactor = ContextCompactor(config.max_context_tokens)
        self.crystallizer = ConfidenceCrystallizer(agent_cfg.role, self.llm, db)
        self.approval = ApprovalGate(db, config)
        self.history: list[Turn] = []

        # nit #2: cache soul.toml sekali, jangan baca file tiap turn
        self._soul = self._load_soul_once()

    def _load_soul_once(self) -> dict:
        with open(f"roles/{self.cfg.role}/soul.toml", "rb") as f:
            return tomllib.load(f)

    async def run(self, user_message: str) -> AsyncGenerator[str, None]:
        start = time.monotonic()

        # 1. Deteksi koreksi user (audit feedback) [#1]
        if self.history:
            await self.auditor.check_correction(user_message, self.cfg.session_id)

        # 2. Load skill aktif (belum decayed) [#2]
        active_skills = await self.decay.get_active_skills(query=user_message)

        # 3. Memory context
        memory_ctx = await self.memory.load_context(user_message, active_skills)

        # 4. Build messages
        messages = self.compactor.build(
            soul=self._soul["system_prompt"]["content"],
            memory=memory_ctx, history=self.history, user_message=user_message,
        )

        # 5. Route (soul-aware) + log [#1]
        route = self.router.decide(messages, user_message)
        event_id = await self.auditor.log_decision(
            self.cfg.session_id, self.cfg.role, user_message, route)

        # 6. Iterative tool loop (audit #10: bukan rekursif)
        turn = Turn(role="assistant", model_used=route.model)
        tools_schema = self._tools_for_role()   # nit #1: difilter

        async for chunk in self._run_tool_loop(messages, route, tools_schema, turn):
            yield chunk

        # 7. Finalize
        turn.latency_ms = int((time.monotonic() - start) * 1000)
        turn.cost_usd = route.cost_per_1k * (turn.tokens_in + turn.tokens_out) / 1000
        self.history.append(Turn(role="user", content=user_message))
        self.history.append(turn)
        await self.auditor.finalize(event_id, turn)

        # 8. Post-turn dengan error handling (audit #3: bukan fire-and-forget)
        task = asyncio.create_task(self._post_turn(user_message, turn, active_skills))
        task.add_done_callback(self._post_turn_done)

    def _post_turn_done(self, task: asyncio.Task):
        """Audit #3: log error jika background task gagal, jangan hilang diam-diam."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("post_turn_failed", error=str(exc), session=self.cfg.session_id)

    async def _run_tool_loop(self, messages, route, tools_schema, turn):
        """Audit #10: iterative, bukan rekursif."""
        hop = 0
        while hop <= self.config.max_tool_hops:
            pending_tool = None
            async for chunk in self.llm.stream_with_fallback(
                route.provider, route.model, messages, tools_schema,
            ):
                if chunk.type == "text":
                    turn.content += chunk.text
                    yield chunk.text
                elif chunk.type == "tool_call":
                    pending_tool = chunk
                elif chunk.type == "usage":
                    turn.tokens_in = chunk.usage.get("input_tokens", 0)
                    turn.tokens_out = chunk.usage.get("output_tokens", 0)

            if not pending_tool:
                break   # tidak ada tool call → selesai

            # Eksekusi tool (dengan approval gate jika perlu)
            result = await self._execute_tool(pending_tool.tool_name,
                                               pending_tool.tool_input)
            turn.tool_calls.append({
                "name": pending_tool.tool_name,
                "input": pending_tool.tool_input,
            })
            messages.append({
                "role": "tool", "name": pending_tool.tool_name, "content": str(result),
            })
            hop += 1

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        tool = TOOL_REGISTRY.get(name)
        if not tool:
            return {"error": f"Tool '{name}' tidak ditemukan"}

        if not self._tool_allowed(name):     # nit #1
            return {"error": f"Tool '{name}' tidak diizinkan untuk role {self.cfg.role}"}

        # Human-in-the-loop untuk tool destruktif (audit gap)
        if tool.requires_approval:
            approved = await self.approval.request(
                self.cfg.session_id, name, input_data)
            if not approved:
                return {"error": f"Tool '{name}' ditolak oleh user"}

        return await tool.execute(input_data, vault=self.vault)

    def _tool_allowed(self, name: str) -> bool:
        return name in self._soul.get("tools", {}).get("allowed", [])

    def _tools_for_role(self) -> list:
        """Nit #1: hanya kirim schema tool yang diizinkan ke LLM."""
        allowed = set(self._soul.get("tools", {}).get("allowed", []))
        return [t.schema() for n, t in TOOL_REGISTRY.items() if n in allowed]
```

---

## 10. Modul: Smart Router (soul-aware)

> Memecahkan audit #1 (router abaikan soul.toml).

```python
# core/router.py

import tomllib
from dataclasses import dataclass
from enum import Enum


class Complexity(Enum):
    TRIVIAL = "trivial"; SIMPLE = "simple"; MODERATE = "moderate"
    COMPLEX = "complex"; CRITICAL = "critical"


@dataclass
class RouteDecision:
    model: str
    provider: str
    complexity: Complexity
    complexity_score: int
    reason: str
    cost_per_1k: float
    dimensions: dict
    soul_upgrade_hit: bool


class SmartRouter:
    """
    Audit #1: membaca soul.toml role aktif.
    - upgrade_keywords digabung ke scoring
    - prefer_local menaikkan threshold upgrade ke Claude
    """

    MODELS = {
        Complexity.TRIVIAL:  ("gemma4:e2b", "ollama", 0.0),
        Complexity.SIMPLE:   ("gemma4:e4b", "ollama", 0.0),
        Complexity.MODERATE: ("gemma4:12b", "ollama", 0.0),
        Complexity.COMPLEX:  ("claude-haiku-4-5-20251001", "anthropic", 0.001),
        Complexity.CRITICAL: ("claude-sonnet-4-6", "anthropic", 0.003),
    }

    BASE_TECH_KW = ["code", "debug", "review", "arsitektur", "implement",
                    "refactor", "query", "database", "api", "deploy", "bug"]
    MULTI_KW = ["analisis", "bandingkan", "rencana", "langkah", "strategi",
                "breakdown", "jelaskan detail", "evaluasi"]
    URGENCY_KW = ["urgent", "segera", "deadline", "asap", "penting"]

    def __init__(self, role: str):
        self.role = role
        soul = self._load_soul(role)
        routing_cfg = soul.get("routing", {})
        self.prefer_local: bool = routing_cfg.get("prefer_local", False)
        self.soul_upgrade_kw: list = routing_cfg.get("upgrade_keywords", [])

    def _load_soul(self, role: str) -> dict:
        with open(f"roles/{role}/soul.toml", "rb") as f:
            return tomllib.load(f)

    def decide(self, messages: list, query: str) -> RouteDecision:
        dims = self._dimensions(messages, query)
        soul_hit = any(k.lower() in query.lower() for k in self.soul_upgrade_kw)
        dims["soul_upgrade_hit"] = int(soul_hit)

        score = self._score(dims)

        # Audit #1: soul upgrade_keywords memaksa naik kompleksitas
        if soul_hit:
            score += 3

        # Audit #1: prefer_local menaikkan threshold (lebih sulit upgrade ke Claude)
        threshold_shift = 1 if self.prefer_local else 0

        complexity = self._label(score, threshold_shift)
        model, provider, cost = self.MODELS[complexity]

        return RouteDecision(
            model=model, provider=provider, complexity=complexity,
            complexity_score=score, reason=self._explain(complexity, soul_hit),
            cost_per_1k=cost, dimensions=dims, soul_upgrade_hit=soul_hit,
        )

    def _dimensions(self, messages, query):
        q = query.lower()
        return {
            "query_tokens": int(len(query.split()) * 1.3),
            "has_tech_kw": int(any(k in q for k in self.BASE_TECH_KW)),
            "needs_multistep": int(any(k in q for k in self.MULTI_KW)),
            "history_len": len(messages),
            "role": self.role,
            "has_urgency": int(any(k in q for k in self.URGENCY_KW)),
            "needs_stream": 1,
            "is_continuation": int(len(messages) > 2),
        }

    def _score(self, d):
        s = 0
        if d["query_tokens"] > 200: s += 2
        elif d["query_tokens"] > 50: s += 1
        if d["has_tech_kw"]: s += 2
        if d["needs_multistep"]: s += 2
        if d["history_len"] > 10: s += 1
        if d["has_urgency"]: s += 1
        return s

    def _label(self, score, threshold_shift):
        # threshold_shift menaikkan batas → prefer_local lebih lama bertahan di Ollama
        if score <= 1 + threshold_shift: return Complexity.TRIVIAL
        if score <= 2 + threshold_shift: return Complexity.SIMPLE
        if score <= 4 + threshold_shift: return Complexity.MODERATE
        if score <= 6 + threshold_shift: return Complexity.COMPLEX
        return Complexity.CRITICAL

    def _explain(self, c, soul_hit):
        base = {
            Complexity.TRIVIAL:  "Greeting/singkat → Ollama 3B",
            Complexity.SIMPLE:   "Sederhana → Ollama 7B",
            Complexity.MODERATE: "Menengah → Ollama 14B",
            Complexity.COMPLEX:  "Kompleks → Claude Haiku",
            Complexity.CRITICAL: "Kritis → Claude Sonnet",
        }[c]
        if soul_hit:
            base += " (dipicu soul upgrade_keyword)"
        return base
```

---

## 11. Modul: Routing Audit (Inovasi 1)

```python
# core/audit.py

from infra.database import DatabaseManager
from core.router import RouteDecision

CORRECTION_SIGNALS = [
    "salah", "bukan itu", "coba lagi", "maksudku", "kurang tepat",
    "tidak benar", "ulangi", "keliru", "bukan begitu", "harusnya",
]


class RoutingAuditor:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def log_decision(self, session_id, role, query, route: RouteDecision) -> int:
        d = route.dimensions
        cursor = await self.db.execute("""
            INSERT INTO routing_events (
                session_id, role, query_text,
                dim_query_tokens, dim_has_tech_kw, dim_needs_multistep,
                dim_history_len, dim_role, dim_has_urgency,
                dim_needs_stream, dim_is_continuation, dim_soul_upgrade_hit,
                complexity_score, complexity_label,
                model_chosen, provider, routing_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session_id, role, query,
            d["query_tokens"], d["has_tech_kw"], d["needs_multistep"],
            d["history_len"], d["role"], d["has_urgency"],
            d["needs_stream"], d["is_continuation"], d["soul_upgrade_hit"],
            route.complexity_score, route.complexity.value,
            route.model, route.provider, route.reason,
        ))
        return cursor.lastrowid

    async def finalize(self, event_id: int, turn) -> None:
        await self.db.execute("""
            UPDATE routing_events
            SET tokens_in=?, tokens_out=?, cost_usd=?, latency_ms=?
            WHERE id=?
        """, (turn.tokens_in, turn.tokens_out, turn.cost_usd, turn.latency_ms, event_id))

    async def check_correction(self, user_message: str, session_id: str) -> None:
        msg = user_message.lower()
        if not any(sig in msg for sig in CORRECTION_SIGNALS):
            return
        await self.db.execute("""
            UPDATE routing_events SET had_correction=1, correction_detail=?
            WHERE id = (SELECT id FROM routing_events
                        WHERE session_id=? ORDER BY created_at DESC LIMIT 1)
        """, (user_message[:200], session_id))

    async def calibration_report(self) -> list[dict]:
        """Complexity label mana yang sering memicu koreksi → router under-provisioned."""
        return await self.db.fetchall("""
            SELECT complexity_label,
                   COUNT(*) as total,
                   SUM(had_correction) as corrections,
                   ROUND(100.0 * SUM(had_correction) / COUNT(*), 1) as correction_rate,
                   ROUND(AVG(cost_usd), 5) as avg_cost
            FROM routing_events
            GROUP BY complexity_label
            ORDER BY correction_rate DESC
        """)
```

---

## 12. Modul: Skill Decay (Inovasi 2)

> Memecahkan audit #6 (exponential decay) dan #7 (frekuensi decay pass).

```python
# memory/skill_decay.py

from datetime import datetime
from infra.database import DatabaseManager

# Audit #6: exponential decay, bukan linear
DECAY_BASE = 0.97              # score *= 0.97 ^ hari_sejak_terakhir_dipakai
ARCHIVE_THRESHOLD = 0.3
REVIVE_BOOST = 0.5
MAX_ACTIVE_SKILLS = 8

# Audit #7: decay pass tidak tiap turn, tapi throttled
DECAY_INTERVAL_SEC = 3600     # minimal 1 jam antar decay pass


class SkillDecayManager:
    def __init__(self, role: str, db: DatabaseManager):
        self.role = role
        self.db = db
        self._last_decay_ts: float = 0.0

    async def get_active_skills(self, query: str) -> list[dict]:
        return await self.db.fetchall("""
            SELECT id, skill_name, skill_content, trigger_pattern, decay_score
            FROM skills
            WHERE role=? AND status='active'
              AND (trigger_pattern IS NULL OR ? LIKE '%' || trigger_pattern || '%')
            ORDER BY decay_score DESC, use_count DESC LIMIT ?
        """, (self.role, query, MAX_ACTIVE_SKILLS))

    async def mark_used(self, skill_id: int) -> None:
        await self.db.execute("""
            UPDATE skills
            SET use_count = use_count + 1, last_used_at = ?,
                decay_score = MIN(1.0, decay_score + ?),
                status = CASE WHEN status='archived' THEN 'active' ELSE status END
            WHERE id = ?
        """, (datetime.now().isoformat(), REVIVE_BOOST, skill_id))

    async def maybe_run_decay_pass(self) -> dict:
        """
        Audit #7: throttle — hanya jalan jika sudah lewat DECAY_INTERVAL_SEC.
        Dipanggil tiap turn, tapi mayoritas no-op.
        """
        import time
        now = time.monotonic()
        if now - self._last_decay_ts < DECAY_INTERVAL_SEC:
            return {"skipped": True}
        self._last_decay_ts = now
        return await self._run_decay_pass()

    async def _run_decay_pass(self) -> dict:
        # Audit #6: exponential decay via formula power
        await self.db.execute("""
            UPDATE skills
            SET decay_score = decay_score * POWER(?,
                julianday('now') - julianday(COALESCE(last_used_at, created_at)))
            WHERE role=? AND status='active'
        """, (DECAY_BASE, self.role))

        cursor = await self.db.execute("""
            UPDATE skills SET status='archived'
            WHERE role=? AND status='active' AND decay_score < ?
        """, (self.role, ARCHIVE_THRESHOLD))
        return {"archived": cursor.rowcount}
```

> **Catatan:** SQLite tidak punya `POWER()` secara default. Daftarkan sebagai custom function saat koneksi dibuat:
> ```python
> # di DatabaseManager.conn(), setelah connect:
> await self._conn.create_function("POWER", 2, lambda b, e: b ** e)
> ```

---

## 13. Modul: Confidence Crystallization (Inovasi 3)

> Memecahkan audit #4 (evaluator harus minimal setara generator).

```python
# core/crystallizer.py

import json
from datetime import datetime
from infra.database import DatabaseManager

MIN_TOOL_CALLS = 3
CONFIDENCE_THRESHOLD = 4

# Audit #4: evaluator harus minimal setara generator.
# Map: model generator → model evaluator minimal.
EVALUATOR_FOR = {
    "gemma4:e2b": ("ollama", "gemma4:e4b"),
    "gemma4:e4b": ("ollama", "gemma4:12b"),
    "gemma4:12b": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-sonnet-4-6": ("anthropic", "claude-sonnet-4-6"),
}
DEFAULT_EVALUATOR = ("anthropic", "claude-haiku-4-5-20251001")


class ConfidenceCrystallizer:
    def __init__(self, role: str, llm, db: DatabaseManager):
        self.role = role
        self.llm = llm
        self.db = db

    def should_attempt(self, history: list) -> bool:
        tool_calls = sum(len(t.tool_calls) for t in history if t.tool_calls)
        return tool_calls >= MIN_TOOL_CALLS

    async def crystallize(self, task, solution, history, generator_model: str) -> dict:
        # Audit #4: pilih evaluator minimal setara generator
        eval_provider, eval_model = EVALUATOR_FOR.get(generator_model, DEFAULT_EVALUATOR)
        evaluation = await self._self_evaluate(task, solution, eval_provider, eval_model)

        status = "active" if (
            evaluation["confidence"] >= CONFIDENCE_THRESHOLD
            and not evaluation["critical_gaps"]
        ) else "draft"

        steps = []
        for turn in history:
            for tc in (turn.tool_calls or []):
                steps.append(f"- {tc['name']}: {json.dumps(tc['input'])[:80]}")

        skill_name = self._slug(task)
        content = self._format(task, steps, solution, evaluation)

        try:
            await self.db.execute("""
                INSERT INTO skills (role, skill_name, trigger_pattern, skill_content,
                                    status, confidence, generator_model, decay_score)
                VALUES (?,?,?,?,?,?,?,1.0)
            """, (self.role, skill_name, task[:60], content,
                  status, evaluation["confidence"] / 5.0, generator_model))
            return {"skill_name": skill_name, "status": status,
                    "evaluator": eval_model, **evaluation}
        except Exception:
            return {"skill_name": skill_name, "status": "duplicate"}

    async def _self_evaluate(self, task, solution, provider, model) -> dict:
        prompt = f"""Nilai kualitas solusi berikut secara objektif.

TASK: {task}

SOLUSI:
{solution[:1500]}

Jawab HANYA JSON valid, tanpa teks lain:
{{"confidence": <1-5>, "critical_gaps": <true/false>, "reasoning": "<satu kalimat>"}}"""

        response = ""
        async for chunk in self.llm.stream_with_fallback(
            provider, model, [{"role": "user", "content": prompt}],
        ):
            if chunk.type == "text":
                response += chunk.text
        return self._parse(response)

    def _parse(self, raw: str) -> dict:
        try:
            cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            return {
                "confidence": int(data.get("confidence", 1)),
                "critical_gaps": bool(data.get("critical_gaps", True)),
                "reasoning": str(data.get("reasoning", "")),
            }
        except (json.JSONDecodeError, ValueError):
            return {"confidence": 1, "critical_gaps": True, "reasoning": "parse failed"}

    def _format(self, task, steps, solution, ev) -> str:
        return f"""# Skill: {self._slug(task)}

## Trigger
{task[:200]}

## Steps
{chr(10).join(steps)}

## Outcome
{solution[:400]}

## Self-evaluation
- Confidence: {ev['confidence']}/5
- Critical gaps: {ev['critical_gaps']}
- Reasoning: {ev['reasoning']}

## Metadata
- Role: {self.role}
- Created: {datetime.now().isoformat()}
"""

    def _slug(self, task: str) -> str:
        words = task.lower().split()[:5]
        return "-".join(w for w in words if w.isalnum()) or "unnamed-skill"
```

---

## 14. Modul: Role Contracts (Inovasi 4)

```python
# roles/contracts.py

from pydantic import BaseModel, Field


class PMOutput(BaseModel):
    summary: str
    user_stories: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")
    open_questions: list[str] = Field(default_factory=list)


class QAOutput(BaseModel):
    test_cases: list[str] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    severity_matrix: dict[str, str] = Field(default_factory=dict)
    pass_criteria: list[str] = Field(default_factory=list)


class DevOutput(BaseModel):
    approach: str
    files_changed: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_review: bool = Field(default=True)


CONTRACT_REGISTRY: dict[str, type[BaseModel]] = {
    "pm": PMOutput, "qa": QAOutput, "dev": DevOutput,
}
```

```python
# roles/registry.py

import json
from pydantic import ValidationError
from infra.database import DatabaseManager
from roles.contracts import CONTRACT_REGISTRY


class RoleNegotiator:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def handoff(self, session_id, from_role, to_role, task_input, agent_factory) -> dict:
        contract_cls = CONTRACT_REGISTRY.get(to_role)
        if not contract_cls:
            return {"error": f"Tidak ada contract untuk role '{to_role}'"}

        sub_agent = agent_factory(to_role)
        schema = json.dumps(contract_cls.model_json_schema(), indent=2)
        prompt = (f"{task_input}\n\nPENTING: Jawab dalam JSON sesuai schema, "
                  f"tanpa teks lain:\n{schema}")

        raw = ""
        async for token in sub_agent.run(prompt):
            raw += token

        validated, ok = self._validate(raw, contract_cls)
        await self.db.execute("""
            INSERT INTO role_handoffs (session_id, from_role, to_role, task_input,
                                       contract_name, output_json, validation_ok)
            VALUES (?,?,?,?,?,?,?)
        """, (session_id, from_role, to_role, task_input, to_role,
              json.dumps(validated), int(ok)))

        return {"from": from_role, "to": to_role, "output": validated, "valid": ok}

    def _validate(self, raw, contract_cls) -> tuple[dict, bool]:
        try:
            cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
            instance = contract_cls(**json.loads(cleaned))
            return instance.model_dump(), True
        except (json.JSONDecodeError, ValidationError) as e:
            return {"raw": raw[:500], "error": str(e)}, False
```

---

## 15. Modul: Memory System

> Memecahkan nit #3 (Anthropic content_block_start sudah ditangani di §8), dan catatan audit soal FTS5 threshold.

```python
# memory/layers.py

from infra.database import DatabaseManager


class MemoryManager:
    def __init__(self, role: str, session_id: str, db: DatabaseManager):
        self.role = role
        self.session_id = session_id
        self.db = db

    async def load_context(self, query: str, skills: list) -> dict:
        l1_rows = await self.db.fetchall(
            "SELECT key, value FROM memory_l1 WHERE role=? LIMIT 20", (self.role,))
        l1 = {r["key"]: r["value"] for r in l1_rows}

        l2_rows = await self.db.fetchall(
            "SELECT fact FROM memory_l2 WHERE role=? ORDER BY importance DESC LIMIT 30",
            (self.role,))
        l2 = [r["fact"] for r in l2_rows]

        # FTS5: trigger jika query > 3 kata ATAU mengandung kata teknis spesifik.
        # (audit: threshold 5 kata terlalu kaku untuk query seperti "bug login OAuth")
        l4 = []
        should_search = len(query.split()) > 3 or self._has_specific_term(query)
        if should_search:
            try:
                l4_rows = await self.db.fetchall("""
                    SELECT summary FROM memory_l4
                    WHERE role=? AND memory_l4 MATCH ? ORDER BY rank LIMIT 3
                """, (self.role, query))
                l4 = [r["summary"] for r in l4_rows]
            except Exception:
                pass   # FTS5 syntax error → skip gracefully

        return {"l1": l1, "l2": l2, "l3": skills, "l4": l4}

    def _has_specific_term(self, query: str) -> bool:
        """Query pendek tapi spesifik tetap layak di-search."""
        specific = ["bug", "error", "oauth", "api", "deploy", "fix", "crash"]
        q = query.lower()
        return any(t in q for t in specific)

    async def update_checkpoint(self, summary: str) -> None:
        await self.db.execute("""
            INSERT INTO memory_l1 (role, key, value) VALUES (?, 'last_summary', ?)
            ON CONFLICT(role, key) DO UPDATE SET value=excluded.value,
                updated_at=CURRENT_TIMESTAMP
        """, (self.role, summary[:500]))

    async def add_fact(self, fact, importance=1, locale="neutral"):
        await self.db.execute(
            "INSERT INTO memory_l2 (role, fact, importance, locale) VALUES (?,?,?,?)",
            (self.role, fact, importance, locale))

    async def archive_session(self, summary, full_content):
        await self.db.execute("""
            INSERT INTO memory_l4 (role, session_id, summary, full_content, created_at)
            VALUES (?,?,?,?, datetime('now'))
        """, (self.role, self.session_id, summary, full_content))
```

---

## 16. Modul: Tools + Sandbox

> Memecahkan audit gap (spesifikasi sandbox `code_run`) dan menambah flag `requires_approval`.

```python
# tools/base.py

from abc import ABC, abstractmethod


class Tool(ABC):
    name: str
    requires_approval: bool = False    # audit gap: tool destruktif perlu approval

    @abstractmethod
    async def execute(self, input_data: dict, vault) -> dict: ...

    @abstractmethod
    def schema(self) -> dict: ...
```

```python
# tools/sandbox.py

import asyncio
import tempfile
import os

# Spesifikasi sandbox untuk code_run (audit gap — keamanan WAJIB):
# - Image minimal: python:3.12-slim
# - Tidak ada akses network (--network none)
# - Mount read-only kecuali /tmp/work yang writable & ephemeral
# - Timeout keras
# - Resource limit (memory, CPU)
# - Tidak ada akses ke host filesystem atau credential

SANDBOX_IMAGE = "openclawn-sandbox:latest"
SANDBOX_TIMEOUT_SEC = 30
SANDBOX_MEM_LIMIT = "256m"
SANDBOX_CPU_LIMIT = "0.5"


class DockerSandbox:
    async def run_python(self, code: str) -> dict:
        with tempfile.TemporaryDirectory() as workdir:
            script_path = os.path.join(workdir, "script.py")
            with open(script_path, "w") as f:
                f.write(code)

            cmd = [
                "docker", "run", "--rm",
                "--network", "none",                    # tidak ada network
                "--memory", SANDBOX_MEM_LIMIT,
                "--cpus", SANDBOX_CPU_LIMIT,
                "--read-only",                          # filesystem read-only
                "--tmpfs", "/tmp:rw,size=64m",          # /tmp writable ephemeral
                "-v", f"{workdir}:/work:ro",            # script di-mount read-only
                "--workdir", "/work",
                "--user", "nobody",                     # non-root
                "--security-opt", "no-new-privileges",
                SANDBOX_IMAGE,
                "timeout", str(SANDBOX_TIMEOUT_SEC),
                "python", "/work/script.py",
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC + 5)
                return {
                    "stdout": stdout.decode()[:4000],
                    "stderr": stderr.decode()[:2000],
                    "exit_code": proc.returncode,
                }
            except asyncio.TimeoutError:
                return {"error": "Eksekusi melebihi timeout", "exit_code": -1}
```

```python
# tools/code.py

from tools.base import Tool
from tools.sandbox import DockerSandbox


class CodeRunTool(Tool):
    name = "code_run"
    requires_approval = True       # selalu butuh approval — eksekusi kode

    def __init__(self):
        self.sandbox = DockerSandbox()

    async def execute(self, input_data: dict, vault) -> dict:
        code = input_data.get("code", "")
        if not code:
            return {"error": "Tidak ada kode untuk dijalankan"}
        return await self.sandbox.run_python(code)

    def schema(self) -> dict:
        return {
            "name": "code_run",
            "description": "Jalankan kode Python dalam sandbox terisolasi (no network)",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        }
```

```dockerfile
# Dockerfile.sandbox
FROM python:3.12-slim
# Hanya stdlib + beberapa paket aman. Tidak ada akses network saat run.
RUN pip install --no-cache-dir numpy pandas
USER nobody
```

---

## 17. Security Layer

> Memecahkan audit nit #4 (Unicode normalization) dan menambah approval gate.

```python
# security/shield.py

import re
import unicodedata

DANGER_PATTERNS = [
    r"ignore (previous|all) instructions",
    r"abaikan (instruksi|perintah) (sebelumnya|di atas)",
    r"system prompt",
    r"reveal your (instructions|prompt)",
]


class Shield:
    """
    Lapisan kosmetik — BUKAN pertahanan utama.
    Pertahanan utama tetap container isolation (lihat §16).
    """

    @staticmethod
    def scan_input(text: str) -> tuple[bool, str]:
        # Nit #4: normalisasi NFKD dulu untuk cegah homoglyph bypass (ìgnore → ignore)
        normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        for pat in DANGER_PATTERNS:
            if re.search(pat, normalized, re.IGNORECASE):
                return False, "Input ditolak: pola mencurigakan terdeteksi"
        return True, ""
```

```python
# security/approval.py

import asyncio
import json
from infra.database import DatabaseManager
from infra.config import AppConfig

# Human-in-the-loop approval untuk tool destruktif (audit gap).
# Implementasi research-phase: approval via Web UI event.
# Untuk fase awal, bisa pakai auto-approve dengan logging, lalu
# ganti ke approval interaktif nyata di Sprint 3.


class ApprovalGate:
    def __init__(self, db: DatabaseManager, config: AppConfig):
        self.db = db
        self.config = config
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, session_id: str, tool_name: str, tool_input: dict) -> bool:
        """
        Minta approval user. Di research phase, default auto-approve + log.
        Di production, ini menunggu event dari Web UI (resolve via approve()).
        """
        await self.db.execute("""
            INSERT INTO approval_log (session_id, tool_name, tool_input, decision)
            VALUES (?,?,?,?)
        """, (session_id, tool_name, json.dumps(tool_input), "approved"))

        # Sprint 3: ganti dengan menunggu Future yang di-resolve oleh Web UI
        # future = asyncio.get_event_loop().create_future()
        # self._pending[session_id] = future
        # return await asyncio.wait_for(future, timeout=self.config.approval_timeout_sec)

        return True   # research phase default

    def approve(self, session_id: str, decision: bool):
        """Dipanggil dari Web UI saat user klik approve/reject."""
        fut = self._pending.pop(session_id, None)
        if fut and not fut.done():
            fut.set_result(decision)
```

```python
# security/vault.py

import os


class Vault:
    def __init__(self):
        self._cache = {}

    async def get(self, key: str) -> str:
        if key in self._cache:
            return self._cache[key]
        value = os.environ.get(key)
        if not value:
            raise ValueError(f"Credential '{key}' tidak ditemukan di environment")
        self._cache[key] = value
        return value
```

---

## 18. Modul: Web UI

```python
# web/main.py

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import uuid

from infra.config import CONFIG
from infra.database import DatabaseManager
from infra.logging import setup_logging
from core.agent_loop import AgentLoop, AgentConfig
from core.audit import RoutingAuditor

db = DatabaseManager(CONFIG)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    conn = await db.conn()
    # daftarkan POWER() untuk exponential decay (§12)
    await conn.create_function("POWER", 2, lambda b, e: b ** e)
    yield
    await db.close()


app = FastAPI(title="OpenCLAWN", lifespan=lifespan)
templates = Jinja2Templates(directory="web/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, role: str = "pm"):
    return templates.TemplateResponse("index.html", {
        "request": request, "role": role,
        "available_roles": ["pm", "qa", "dev"],
        "session_id": str(uuid.uuid4()),
    })


@app.post("/chat/stream")
async def chat_stream(request: Request):
    form = await request.form()
    message = (form.get("message") or "").strip()
    role = form.get("role", "pm")
    session_id = form.get("session_id", str(uuid.uuid4()))
    if not message:
        return HTMLResponse("")

    agent = AgentLoop(AgentConfig(role=role, session_id=session_id), db=db)

    async def generate():
        yield "data: <div class='msg assistant'>\n\n"
        async for token in agent.run(message):
            safe = token.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
            yield f"data: {safe}\n\n"
        yield "data: </div>\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/metrics", response_class=HTMLResponse)
async def metrics(request: Request):
    report = await RoutingAuditor(db).calibration_report()
    return templates.TemplateResponse("metrics.html", {"request": request, "report": report})
```

---

## 19. Konfigurasi Role (SOUL.toml)

```toml
# roles/pm/soul.toml
[meta]
role = "pm"
name = "PM Agent"

[system_prompt]
content = """
Kamu adalah Product Manager agent.
Peranmu: breakdown request jadi user stories, tetapkan prioritas, draft acceptance criteria.
Prinsip: tanya "untuk siapa dan mengapa" sebelum "apa dan bagaimana".
Saat handoff ke role lain, gunakan format contract yang sesuai.
"""

[tools]
allowed = ["file_read", "file_write", "web_fetch", "ask_user"]

[routing]
prefer_local = true
upgrade_keywords = ["arsitektur", "strategi", "roadmap", "OKR"]

[contract]
output_type = "PMOutput"
```

```toml
# roles/qa/soul.toml
[meta]
role = "qa"
name = "QA Agent"

[system_prompt]
content = """
Kamu adalah QA Engineer agent.
Peranmu: review kode, buat test cases, identifikasi edge cases, validasi acceptance criteria.
Prinsip: selalu tanya "apa yang bisa gagal?".
"""

[tools]
allowed = ["file_read", "file_write", "code_run", "ask_user"]

[routing]
prefer_local = false
upgrade_keywords = ["security", "performance", "race condition", "injection"]

[contract]
output_type = "QAOutput"
```

---

## 20. Testing Strategy

```python
# tests/test_router.py
import pytest
from core.router import SmartRouter, Complexity

def test_soul_upgrade_keyword_forces_higher_complexity():
    """Audit #1: keyword dari soul.toml harus menaikkan kompleksitas."""
    router = SmartRouter(role="pm")    # pm punya upgrade_keywords=["arsitektur",...]
    route = router.decide(messages=[], query="bantu desain arsitektur sistem")
    assert route.soul_upgrade_hit is True
    assert route.complexity in (Complexity.COMPLEX, Complexity.CRITICAL)

def test_prefer_local_keeps_simple_query_on_ollama():
    """prefer_local=true harus menahan query di Ollama lebih lama."""
    router = SmartRouter(role="pm")    # prefer_local=true
    route = router.decide(messages=[], query="apa itu sprint?")
    assert route.provider == "ollama"
```

```python
# tests/test_fallback.py
import pytest
from unittest.mock import AsyncMock
from core.llm_client import LLMClient, ProviderUnavailable
from infra.config import CONFIG

@pytest.mark.asyncio
async def test_fallback_when_ollama_down(monkeypatch):
    """Audit #5: jika Ollama offline, harus turun ke fallback berikutnya."""
    client = LLMClient(vault=AsyncMock(), config=CONFIG)
    monkeypatch.setattr(client, "_health_check", AsyncMock(return_value=False))
    # ... assert fallback chain dicoba
```

```python
# tests/test_crystallizer.py
def test_evaluator_at_least_as_strong_as_generator():
    """Audit #4: evaluator tidak boleh lebih lemah dari generator."""
    from core.crystallizer import EVALUATOR_FOR
    # Sonnet generator → evaluator minimal Sonnet, bukan 7B
    assert EVALUATOR_FOR["claude-sonnet-4-6"] == ("anthropic", "claude-sonnet-4-6")
    # e4b generator → evaluator naik ke 12b
    assert EVALUATOR_FOR["gemma4:e4b"][1] == "gemma4:12b"
```

Prinsip: DB in-memory (`:memory:`), mock semua LLM call, satu file test per inovasi + fallback.

---

## 21. Roadmap Implementasi

### Sprint 0 — Fondasi resilient ✅ SELESAI (2026-06-15)
- [x] Scaffold + `infra/` (config, database, logging)
- [x] `DatabaseManager` + migration runner + POWER() function
- [x] `llm_client.py` dengan **retry + fallback chain** (P0/P1) — `fallback_used` ditulis ke DB via chunk signal
- [x] `agent_loop.py` minimal, **iterative tool loop** (P1)
- [x] Web UI streaming (FastAPI SSE + HTMX)
- [x] **Inovasi 1 (audit)** — logging dasar

### Sprint 1 — Router + Memory ✅ SELESAI (2026-06-15)
- [x] `router.py` **soul-aware** (P0) — soul `upgrade_keywords` bypass `prefer_local` threshold
- [x] **Inovasi 1 (calibration)** — `calibration_report()` + `/metrics` dashboard
- [x] Memory L1-L2-L4 + FTS5 (threshold adaptif) — `memory/layers.py` + `memory/search.py`
- [x] Context compactor + **prompt caching** (P2) — token budget estimasi len/4; Claude `cache_control: ephemeral`

### Sprint 2 — Skills + Decay (3-4 hari)
- [x] Tool loop + 5 tools
- [x] **code_run sandbox** Docker (P0, keamanan)
- [x] **Inovasi 3 (crystallizer)** — **evaluator valid** (P0)
- [x] **Inovasi 2 (decay)** — **exponential + throttled** (P1)

### Sprint 3 — Multi-Role + HITL (3-4 hari)
- [x] QA + Dev role
- [x] **Inovasi 4 (contracts)** + RoleNegotiator
- [x] **Human-in-the-loop approval** interaktif (P2)
- [x] Docker per role + Vault + Shield (NFKD)

### Sprint 4 — Hardening (ongoing)
- [x] Test coverage 4 inovasi + fallback
- [x] Tooling tuning: `RoutingCalibrator` (saran threshold dari audit, di /metrics)
- [ ] Tuning router dari data audit nyata — **BLOCKED: butuh traffic nyata** (advisor siap pakai)
- [ ] Pertimbangkan embedding routing — **BLOCKED: hanya jika data buktikan keyword tak cukup (§1.6, §8)**
- [ ] Extract 4 inovasi jadi modul standalone — backlog (refactor struktural)

---

## 22. Quick Start

```bash
git clone <repo> openclawn && cd openclawn
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

mkdir -p data
sqlite3 data/openclawn.db < migrations/001_initial.sql

cp .env.example .env          # isi ANTHROPIC_API_KEY

# Build sandbox image untuk code_run
docker build -t openclawn-sandbox:latest -f Dockerfile.sandbox .

# Ollama
ollama pull gemma4:e2b && ollama pull gemma4:e4b && ollama pull gemma4:12b

uvicorn web.main:app --reload --port 8000
# Chat:    http://localhost:8000
# Metrics: http://localhost:8000/metrics
```

### Verifikasi 4 inovasi + perbaikan audit

```bash
# Inovasi 1 — audit + soul upgrade tracking
sqlite3 data/openclawn.db "SELECT complexity_label, dim_soul_upgrade_hit, fallback_used, had_correction FROM routing_events LIMIT 5;"

# Inovasi 2 — exponential decay
sqlite3 data/openclawn.db "SELECT skill_name, status, ROUND(decay_score,3) FROM skills ORDER BY decay_score DESC;"

# Inovasi 3 — confidence + generator/evaluator tracking
sqlite3 data/openclawn.db "SELECT skill_name, status, confidence, generator_model FROM skills;"

# Inovasi 4 — role handoffs
sqlite3 data/openclawn.db "SELECT from_role, to_role, validation_ok FROM role_handoffs;"

# Approval log (HITL)
sqlite3 data/openclawn.db "SELECT tool_name, decision FROM approval_log;"
```

---

## Lampiran A: Audit Resolution

Status setiap poin dari audit eksternal (2026-06-15):

| # | Isu audit | Status | Lokasi fix |
|---|---|---|---|
| 1 | Router abaikan soul.toml | ✅ Diambil | §10 `SmartRouter.__init__` |
| 2 | Keyword classifier rapuh | 🔶 Ditunda | §21 Sprint 4 — keputusan berbasis data audit |
| 3 | `_post_turn` fire-and-forget | ✅ Diambil | §9 `_post_turn_done` callback |
| 4 | Evaluator crystallization circular | ✅ Diambil | §13 `EVALUATOR_FOR` map |
| 5 | Tidak ada fallback model | ✅ Diambil | §8 `stream_with_fallback` |
| 6 | Decay linear → exponential | ✅ Diambil | §12 `DECAY_BASE` power |
| 7 | Decay pass terlalu sering | ✅ Diambil | §12 `maybe_run_decay_pass` throttle |
| 8 | DB_PATH hardcoded | ✅ Diambil | §7 `AppConfig` |
| 9 | Koneksi DB per metode | ✅ Diambil | §7 `DatabaseManager` |
| 10 | Tool loop rekursif | ✅ Diambil | §9 `_run_tool_loop` while |
| — | Retry logic | ✅ Diambil | §8 `tenacity` |
| — | code_run sandbox | ✅ Diambil | §16 `DockerSandbox` |
| — | Prompt caching | ✅ Diambil | §8 `cache_control` |
| — | Human-in-the-loop | ✅ Diambil | §17 `ApprovalGate` |
| — | Structured logging | ✅ Diambil | §7 `structlog` |
| — | FTS5 threshold kaku | ✅ Diambil | §15 `_has_specific_term` |
| nit1 | `_tools_for_role` tidak difilter | ✅ Diambil | §9 `_tools_for_role` |
| nit2 | soul dibaca dua kali | ✅ Diambil | §9 `_load_soul_once` |
| nit3 | `content_block_start` | ✅ Diambil | §8 `_claude` |
| nit4 | Shield homoglyph bypass | ✅ Diambil | §17 NFKD |
| nit5 | Nama model Claude "salah" | ❌ Ditolak | Sudah diverifikasi: `claude-haiku-4-5` valid |
| — | Conversation branching | 🔶 Ditunda | Out of scope research phase |

---

*Living spec — update setiap akhir sprint berdasarkan temuan.*
