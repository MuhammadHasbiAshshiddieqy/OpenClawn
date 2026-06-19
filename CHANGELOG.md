# Changelog

All notable changes to OpenCLAWN are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/) with pre-release suffixes during the research phase.

## [0.3.0-alpha] — 2026-06-19

First tagged pre-release. Feature-complete against the v0.3 core spec, but still in
the **research/experiment phase**: single-user, no auth, APIs may shift before a
stable `0.3.0`. All tests mock the LLM/Docker/Ollama layers — expect to validate
your own local setup end-to-end.

### The 4 core innovations
- **Routing audit + self-calibration** — every routing decision is logged with its
  8 dimensions and later checked for correction; `/metrics` surfaces tuning
  recommendations you apply (or revert) by hand.
- **Skill decay** — skills fade exponentially (`0.97 ^ days_idle`) and archive below
  threshold; reuse revives them.
- **Confidence-gated crystallization** — the agent self-evaluates a solution before
  storing it as a skill; an evaluator no weaker than the generator gates it.
- **Role output contracts** — typed Pydantic handoffs (PM/Dev/QA/Data/Security);
  invalid output degrades gracefully (`validation_ok=0`) instead of crashing.

### Multi-agent conversation
- Three patterns over one orchestrator: **pipeline** (sequential handoff), **debate**
  (round-robin N rounds), **orchestrator** (lead delegates dynamically with a fixed
  fallback). Live **stop** and **interject** mid-conversation. Transcripts archived
  to `/conversations`.

### Hybrid LLM + routing
- Local-first fallback chain (Ollama → Gemini) with graceful degradation; raw httpx,
  no vendor SDKs (audit transparency). Editable tier→model map via `/router`; manual
  model override via `/settings`.

### Tooling (25 tools)
- Files (read/write/edit/patch/glob/grep, batch `read_many`), code & shell **only**
  inside a Docker sandbox (`--network none`, `--read-only`, non-root,
  `no-new-privileges`), documents (`doc_write` docx/pptx/xlsx/md, `pdf_write`,
  `pdf_read`), git (read-only status/diff/log via sandbox), `todo_write`, web
  (`web_fetch`/`web_search`/`http_request`).

### Security
- `code_run`/`shell_run` never execute on the host — Docker sandbox only.
- Credentials injected from `Vault` on outbound requests only; never in
  context/prompt/DB, and scrubbed from logs.
- **Anti-SSRF guard** on `web_fetch`/`http_request`: rejects loopback, RFC1918, and
  link-local hosts (incl. cloud metadata `169.254.169.254`), resolving DNS to catch
  rebinding.
- Central tool safety net: schema validation + timeout + graceful error + telemetry.

### Observability & ops
- `/metrics` (routing calibration + tool telemetry), `/skills` (decay curves +
  crystallization log), `/conversations` (multi-agent archive).
- GitHub Actions CI (ruff format + lint + pytest) with an enforced `uv.lock` for
  reproducible builds.

### Quality
- **325 tests** passing, `ruff` clean.
