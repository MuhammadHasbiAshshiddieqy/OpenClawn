# `tests/` — Panduan Testing

OpenCLAWN menggunakan pytest + pytest-asyncio. Semua test berjalan tanpa koneksi Ollama/Claude nyata.

---

## Aturan Testing

- **DB:** selalu `:memory:`. Jangan sentuh `data/openclawn.db`
- **LLM:** selalu mock dengan `unittest.mock.AsyncMock`. Test tidak boleh memanggil provider nyata
- `asyncio_mode = "auto"` sudah dikonfigurasi di `pyproject.toml` — semua test async berjalan otomatis
- `@pytest.mark.asyncio` tetap ditulis untuk kejelasan

```bash
pytest tests/ -v          # jalankan semua test
pytest tests/test_router.py -v   # satu file saja
```

---

## Daftar File Test

### `tests/test_router.py`

Test untuk `core/router.py` (Inovasi 1 — routing).

| Test | Yang Diverifikasi |
|---|---|
| `test_soul_upgrade_keyword_raises_complexity` | Keyword dari `soul.toml` menaikkan complexity |
| `test_prefer_local_stays_on_ollama` | `prefer_local=True` menahan query di Ollama |
| `test_soul_hit_overrides_prefer_local` | Soul upgrade_keyword bypass `prefer_local` |
| `test_trivial_query_routes_to_e2b` | Query pendek → gemma4:e2b |
| `test_tech_keyword_raises_score` | Kata teknis menaikkan skor |
| `test_all_dimensions_in_route_decision` | Semua 8 dimensi ada di `RouteDecision.dimensions` |

---

### `tests/test_fallback.py`

Test untuk `core/llm_client.py` — fallback chain.

| Test | Yang Diverifikasi |
|---|---|
| `test_ollama_offline_falls_to_haiku` | Ollama down → turun ke claude-haiku |
| `test_fallback_chunk_emitted` | `LLMChunk(type="fallback")` di-yield saat fallback |
| `test_all_providers_fail_raises` | Semua provider gagal → `ProviderUnavailable` |
| `test_no_retry_on_logic_error` | Error logika tidak di-retry |
| `test_ollama_health_check_false_when_offline` | Health check return False jika offline |

---

### `tests/test_skill_decay.py`

Test untuk `memory/skill_decay.py` (Inovasi 2).

| Test | Yang Diverifikasi |
|---|---|
| `test_skill_decays_over_time` | Skill yang tak terpakai menurun skornya |
| `test_skill_archived_below_threshold` | Skor < 0.3 → status berubah jadi `archived` |
| `test_mark_used_revives_archived_skill` | `mark_used()` mengembalikan skill ke `active` |
| `test_mark_used_increases_score` | `mark_used()` menaikkan `decay_score` |
| `test_decay_throttled` | `maybe_run_decay_pass()` skip jika interval belum lewat |
| `test_get_active_skills_excludes_archived` | Skill archived tidak muncul di `get_active_skills` |

---

### `tests/test_crystallizer.py`

Test untuk `core/crystallizer.py` (Inovasi 3).

| Test | Yang Diverifikasi |
|---|---|
| `test_evaluator_not_weaker_than_generator` | Evaluator ≥ generator untuk semua model |
| `test_low_confidence_stored_as_draft` | Confidence < 4 → status `draft` |
| `test_high_confidence_stored_as_active` | Confidence ≥ 4, no gaps → status `active` |
| `test_critical_gaps_forces_draft` | `critical_gaps=True` → status `draft` walau confidence tinggi |
| `test_should_attempt_requires_min_tool_calls` | `should_attempt()` False jika tool call < 3 |
| `test_parse_failure_falls_safe` | JSON parse gagal → confidence 1, draft |
| `test_crystallize_duplicate_is_graceful` | Insert duplikat tidak crash, return `"duplicate"` |

---

### `tests/test_contracts.py`

Test untuk `roles/contracts.py` dan `roles/registry.py` (Inovasi 4).

| Test | Yang Diverifikasi |
|---|---|
| `test_valid_pm_output_passes` | PMOutput dengan data valid lolos validasi |
| `test_invalid_priority_fails` | Priority bukan `low/medium/high` → ValidationError |
| `test_valid_qa_output_passes` | QAOutput valid |
| `test_valid_dev_output_passes` | DevOutput valid |
| `test_handoff_invalid_output_no_crash` | Output tidak valid → `validation_ok=False`, tidak crash |
| `test_handoff_logged_to_db` | Handoff selalu disimpan ke DB (valid maupun tidak) |
| `test_unknown_role_returns_error` | Role tidak ada di registry → error dict |

---

### `tests/test_audit.py`

Test untuk `core/audit.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_log_decision_returns_id` | `log_decision()` return `lastrowid` valid |
| `test_finalize_updates_record` | `finalize()` update token/cost/latency |
| `test_check_correction_marks_previous` | Kata koreksi → update `had_correction=1` |
| `test_no_correction_no_update` | Pesan normal → `had_correction` tetap 0 |
| `test_calibration_report_structure` | Report punya field yang diharapkan |

---

### `tests/test_memory.py`

Test untuk `memory/layers.py` dan `memory/search.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_update_checkpoint_upsert` | UPSERT benar, tidak duplikasi |
| `test_load_context_l1_populated` | L1 tersedia setelah `update_checkpoint` |
| `test_fts5_search_returns_relevant` | FTS5 search menemukan arsip yang relevan |
| `test_fts5_skipped_for_short_query` | Query < 3 kata tidak memicu FTS5 |
| `test_session_search_graceful_on_error` | FTS5 error → return `[]`, tidak crash |

---

### `tests/test_memory_wiring.py`

Regression guard untuk bug yang ditemukan di Sprint 4: `_post_turn` tidak memanggil write memori.

| Test | Yang Diverifikasi |
|---|---|
| `test_post_turn_writes_l1_checkpoint` | L1 tertulis setelah tiap turn dengan konten |
| `test_post_turn_empty_content_no_checkpoint` | Turn tanpa konten tidak menulis L1 kosong |
| `test_post_turn_archives_l4_after_threshold` | L4 diarsipkan setelah ambang turn tercapai |
| `test_post_turn_no_archive_below_threshold` | L4 tidak diarsipkan jika history masih pendek |
| `test_repeated_archive_no_duplicates` | Arsip idempoten: tidak menumpuk duplikat |
| `test_written_memory_is_readable_next_turn` | Memori yang ditulis terbaca di `load_context` berikutnya |

---

### `tests/test_security.py`

Test untuk `security/`.

| Test | Yang Diverifikasi |
|---|---|
| `test_shield_blocks_prompt_injection` | Input dengan "ignore previous instructions" diblokir |
| `test_shield_allows_normal_input` | Input normal lolos shield |
| `test_shield_nfkd_normalized` | Homoglyph (unicode lookalike) terdeteksi |
| `test_vault_returns_env_value` | `Vault.get()` baca dari `os.environ` |
| `test_vault_raises_if_missing` | Credential tidak ada → `ValueError` |
| `test_approval_gate_resolve_approves` | `resolve(True)` → `request()` return True |
| `test_approval_gate_resolve_rejects` | `resolve(False)` → `request()` return False |
| `test_approval_gate_timeout_denies` | Timeout → fail-safe DENY |
| `test_approval_pending_list` | `pending_list()` menampilkan approval yang menunggu |

---

### `tests/test_tools.py`

Test untuk `tools/`.

| Test | Yang Diverifikasi |
|---|---|
| `test_file_read_returns_content` | `FileReadTool` baca file yang ada |
| `test_file_read_not_found` | File tidak ada → error dict (tidak crash) |
| `test_file_write_creates_file` | `FileWriteTool` tulis konten |
| `test_web_fetch_returns_content` | `WebFetchTool` fetch URL (mocked) |
| `test_code_run_requires_approval` | `CodeRunTool.requires_approval == True` |
| `test_file_write_requires_approval` | `FileWriteTool.requires_approval == True` |
| `test_file_read_no_approval_needed` | `FileReadTool.requires_approval == False` |
| `test_approval_called_for_destructive_tool` | Tool destruktif memanggil `ApprovalGate.request()` |

---

### `tests/test_calibration.py`

Test untuk `core/calibration.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_under_provisioned_detected` | Correction rate tinggi → rekomendasi `under_provisioned` |
| `test_over_provisioned_detected` | Cloud label, rate rendah → rekomendasi `over_provisioned` |
| `test_small_sample_ignored` | N < min_sample → tidak ada rekomendasi |
| `test_summary_has_required_keys` | `summary()` punya `total_events`, `has_enough_data`, `recommendations` |
| `test_no_recommendations_for_healthy_routing` | Routing sehat → list rekomendasi kosong |

---

### `tests/test_compactor.py`

Test untuk `core/compactor.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_history_trimmed_when_over_budget` | History lama dipotong jika melebihi token budget |
| `test_system_prompt_always_included` | System prompt selalu ada di messages[0] |
| `test_memory_sections_in_system_prompt` | L1/L2/L3/L4 muncul di system prompt jika ada |

---

### `tests/test_web.py`

Smoke test untuk endpoints Web UI.

| Test | Yang Diverifikasi |
|---|---|
| `test_index_returns_200` | `GET /` return 200 |
| `test_metrics_returns_200` | `GET /metrics` return 200 |
| `test_approve_invalid_params` | `POST /approve` tanpa params → error JSON |
| `test_approvals_returns_list` | `GET /approvals` return `{"pending": [...]}` |

---

## Pola Test Async

```python
import pytest
from unittest.mock import AsyncMock, patch
from infra.config import AppConfig
from infra.database import DatabaseManager

@pytest.fixture
async def db():
    cfg = AppConfig(db_path=":memory:")
    manager = DatabaseManager(cfg)
    conn = await manager.conn()
    with open("migrations/001_initial.sql") as f:
        await conn.executescript(f.read())
        await conn.commit()
    yield manager
    await manager.close()

@pytest.mark.asyncio
async def test_sesuatu(db):
    # Arrange
    ...
    # Act
    result = await some_func(db)
    # Assert
    assert result == expected
```

## Mock LLM

```python
from unittest.mock import AsyncMock, patch

async def _mock_stream(*args, **kwargs):
    yield LLMChunk(type="text", text='{"confidence":5,"critical_gaps":false,"reasoning":"ok"}')

with patch.object(llm_client, "stream_with_fallback", side_effect=_mock_stream):
    result = await crystallizer.crystallize(...)
```
