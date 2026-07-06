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
| `test_english_tech_keyword_detected` | Keyword teknis Inggris terdeteksi (default config ID+EN) |
| `test_english_multistep_keyword_detected` | Keyword multi-langkah Inggris (analyze/compare) terdeteksi |
| `test_soul_can_add_locale_keywords` | soul.toml dapat menambah keyword bahasa lain (Spanyol) tanpa edit core (§1.5) |
| `test_unknown_language_falls_back_to_neutral_signal` | Bahasa tanpa keyword cocok tetap dirute oleh sinyal netral (panjang) |
| `test_code_signal_detected_without_keywords` | Sinyal struktural: kode terdeteksi tanpa keyword (lintas bahasa) |
| `test_code_fence_raises_score` | Code fence ` ``` ` menaikkan skor (universal) |
| `test_plain_text_no_code_signal` | Teks biasa → `has_code_signal=0` (tanpa false-positive) |
| `test_detect_script_cjk` / `_latin_ascii` / `_arabic` | `_detect_script` mengenali sistem tulisan via Unicode |
| `test_language_bump_off_by_default` | Language bump OFF default (opt-in, tak menambah biaya) |
| `test_language_bump_raises_tier_when_enabled` | Aktif: script di luar tier lokal → naik tier; latin tak di-bump |
| `test_pm_prd_request_routes_to_cloud_not_local_reasoning_model` | Regresi bug "No answer": PRD/dokumen di soul PM asli menembus `COMPLEX` (cloud), bukan berhenti di tier lokal reasoning-heavy |
| `test_pm_unrelated_query_still_stays_local` | Fix PRD tidak menaikkan biaya untuk query PM lain (tetap di Ollama) |

---

### `tests/test_router_config.py`

Test `RouterConfigStore` + `SmartRouter.model_map` (pilih model tiap tier via UI).

| Test | Yang Diverifikasi |
|---|---|
| `test_default_map_when_unset` | Tanpa override → peta = `MODELS` default |
| `test_set_and_get_partial_override` | Override sebagian tier; sisanya tetap default |
| `test_unknown_provider_rejected` | Provider tak dikenal tidak disimpan |
| `test_reset_clears_override` | `reset()` → kembali default |
| `test_corrupt_value_falls_safe_to_default` | JSON korup → fail-safe ke default penuh |
| `test_partial_entry_missing_model_ignored` | Entry tanpa model diabaikan |
| `test_router_uses_overridden_model_for_tier` | `decide()` pakai model_map override untuk tier |
| `test_router_falls_back_to_models_if_tier_missing` | model_map parsial → fallback `MODELS`, tak KeyError |

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
| `test_stale_unproven_draft_archived` | Draft tua & tak terbukti → diarsipkan (bukan dihapus) |
| `test_recent_draft_not_archived` | Draft baru tidak diarsipkan |
| `test_proven_draft_not_archived` | Draft tua tapi terbukti (success_count>0) tidak diarsipkan |
| `test_draft_cleanup_disabled_when_zero` | `draft_stale_days=0` menonaktifkan cleanup |

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
| `test_crystallization_logged_with_decision` | Percobaan tercatat di `crystallization_log` (status/confidence/model) |
| `test_crystallization_log_records_draft` | Draft juga tercatat (yang menarik untuk ditinjau) |

---

### `tests/test_contracts.py`

Test untuk `roles/contracts.py` dan `roles/registry.py` (Inovasi 4), plus loadability semua soul.

| Test | Yang Diverifikasi |
|---|---|
| `test_pm_output_valid` | PMOutput dengan data valid lolos validasi |
| `test_pm_output_invalid_priority` | Priority bukan `low/medium/high` → ValidationError |
| `test_qa_output_valid` | QAOutput valid |
| `test_dev_output_requires_approach` | DevOutput tanpa `approach` → ValidationError |
| `test_data_output_valid` | DataOutput minimal lolos; `confidence` default `medium` |
| `test_data_invalid_confidence` | `confidence` di luar set → ValidationError |
| `test_security_output_valid_and_risk_levels` | SecurityOutput lolos; `risk_level` menerima `critical` |
| `test_security_invalid_risk_level` | `risk_level` di luar set → ValidationError |
| `test_contract_registry_has_all_roles` | Registry punya pm/qa/dev/data/security |
| `test_all_souls_loadable_and_well_formed` | Tiap `soul.toml` bisa di-parse + field wajib ada |
| `test_soul_output_type_matches_registry` | `output_type` soul cocok dengan contract di registry |
| `test_security_role_is_read_only` | Soul security tak punya tool tulis/eksekusi/network |
| `test_valid_output_passes_validation` | Output JSON valid → `validation_ok=1`, tersimpan di DB |
| `test_invalid_output_does_not_crash` | Output tidak valid → `validation_ok=0`, tidak crash |
| `test_unknown_role_returns_error` | Role tidak ada di registry → error dict |

---

### `tests/test_conversation.py`

Test untuk `core/conversation.py` (multi-agent conversation). Seam = fake `agent_factory` yang `run()`-nya yield `AgentEvent` skrip.

| Test | Yang Diverifikasi |
|---|---|
| `test_pipeline_strategy_orders_roles` | Pipeline jalan urut pm→dev→qa lalu `strategy_done` |
| `test_turn_boundary_events_emitted` | Satu event `type="turn"` per giliran, role+index benar |
| `test_debate_strategy_round_robin` | Debate `rounds=2` → urutan round-robin penuh |
| `test_orchestrator_dynamic_delegation` | Lead delegasi via JSON → worker dipanggil → lead `done` |
| `test_orchestrator_fallback_when_unparseable` | Directive tak terbaca → fallback lead→workers |
| `test_max_conversation_turns_respected` | Kena cap → `conversation_end` reason `max_turns` |
| `test_stop_halts_between_turns` | STOP antar-giliran → giliran berikutnya tak jalan, reason `stopped` |
| `test_interject_consumed_in_next_turn` | Interjection muncul di prompt giliran berikutnya |
| `test_pipeline_contract_valid` | Output JSON valid → `validation_ok=1` di `role_handoffs` |
| `test_pipeline_contract_degrades_on_garbage` | Output sampah → `validation_ok=0` TAPI pipeline lanjut |
| `test_control_interjection_queue` | Antrian interjection FIFO, kosong diabaikan |
| `test_state_dataclass_defaults` | `ConversationState` default: `turn_index=0`, `last_output=None` |
| `test_make_strategy_pipeline_preserves_order` | Pipeline memakai urutan `participants` apa adanya |
| `test_make_strategy_debate_uses_rounds` | Debate meneruskan jumlah ronde dari UI |
| `test_make_strategy_orchestrator_lead_is_first_participant` | Lead = `participants[0]` (bukan harus PM), worker = sisanya |
| `test_make_strategy_orchestrator_default_pm_lead` | Tanpa `participants` → default config, PM jadi lead |
| `test_conversation_persisted_on_completion` | Run selesai → baris di `conversations` dengan transkrip & metadata benar |
| `test_conversation_persisted_once_per_run` | Tepat satu baris arsip per run (persist hanya di `conversation_end`) |
| `test_make_strategy_unknown_pattern_raises` | Pattern tak dikenal → `ValueError` |
| `test_orchestrator_non_pm_lead_runs` | End-to-end: lead `dev` bicara lebih dulu |

---

### `tests/test_activity.py`

Test untuk `core/activity.py` (Activity Timeline — agregasi lintas tabel, terinspirasi Multica).

| Test | Yang Diverifikasi |
|---|---|
| `test_timeline_aggregates_all_sources` | Gabung route/tool/handoff/crystallize/conversation jadi satu linimasa |
| `test_timeline_sorted_newest_first` | Urut global terbaru-dulu (lintas sumber) |
| `test_timeline_filter_by_role` | `role=` memfokuskan satu peran; conversation hanya saat `role=None` |
| `test_timeline_outcome_normalized` | Outcome diseragamkan (ok/valid/corrected/degraded dll) |
| `test_timeline_empty_returns_list` | Tanpa data → `[]`, tak crash |
| `test_timeline_unknown_role_filters_to_empty` | Role tak dikenal → kosong |
| `test_timeline_respects_limit` | `limit` menjepit jumlah, tetap terbaru-dulu |

---

### `tests/test_blocker.py`

Test untuk `tools/blocker.py` (`report_blocker`) + tampil di linimasa.

| Test | Yang Diverifikasi |
|---|---|
| `test_report_blocker_persists` | Tulis `agent_blockers` (summary/severity/role/status open) |
| `test_report_blocker_no_approval` | `requires_approval=False` (tabel internal) |
| `test_report_blocker_requires_session` | Tanpa `_session_id` → error |
| `test_report_blocker_requires_summary` | Summary kosong → error |
| `test_report_blocker_rejects_bad_severity` | Severity tak valid → error, tak menulis |
| `test_report_blocker_default_severity_medium` | Default severity `medium` |
| `test_blocker_appears_in_timeline` | Muncul di `ActivityTimeline` sebagai kind `blocker` |

---

### `tests/test_autopilot.py`

Test untuk `core/autopilot.py` (store, scheduler) + **gating keamanan** mode autopilot.

| Test | Yang Diverifikasi |
|---|---|
| `test_create_and_list` | Buat autopilot → tersimpan dengan next_run_at |
| `test_interval_floored_to_minimum` | Interval < 60s dinaikkan ke minimum |
| `test_toggle_and_delete` | Enable/disable & hapus |
| `test_due_returns_only_past_and_enabled` | `due()` hanya aktif & `next_run_at` lewat |
| `test_mark_ran_reschedules_forward` | Reschedule dari sekarang (misfire-safe, tak menumpuk) |
| `test_scheduler_runs_due_via_runner` | Scheduler jalankan yang due lewat `runner`, lalu reschedule |
| `test_scheduler_records_run_and_survives_runner_error` | Runner error → `autopilot_runs` status `error`, scheduler tak mati |
| `test_autopilot_queues_proposal_not_executes` | **§17:** tool butuh-approval di autopilot → DIANTRI proposal, `ApprovalGate.request` TIDAK dipanggil |
| `test_interactive_mode_still_requests_approval` | Tanpa autopilot → approval tetap diminta (tak ada regresi) |

---

### `tests/test_skill_pack.py`

Test untuk `core/skill_pack.py` (berbagi skill) + **5 lapis keamanan impor** (lihat juga `test_skill_scanner.py` untuk lapis scanner).

| Test | Yang Diverifikasi |
|---|---|
| `test_export_renders_active_skills` | Ekspor skill active → Markdown berfrontmatter + hash |
| `test_export_excludes_non_active` | Draft/archived tak diekspor |
| `test_export_filter_by_role` | Ekspor per role |
| `test_export_empty_when_no_skills` | Tanpa skill → string kosong |
| `test_export_then_import_roundtrip` | Ekspor → impor ke DB lain → konten utuh |
| `test_import_lands_as_draft_not_active` | **Lapis 3:** impor → `draft` (tak auto-context), visibility `inherited` |
| `test_import_blocks_prompt_injection` | **Lapis 2:** konten berpola injeksi ditolak Shield, tak tersimpan |
| `test_import_rejects_tampered_hash` | **Lapis 4:** hash tak cocok → ditolak |
| `test_import_accepts_correct_hash` | Hash cocok → diterima |
| `test_import_url_blocks_internal_host` | **Lapis 1:** impor URL ke host internal ditolak SSRF |
| `test_import_url_rejects_non_http` | Scheme non-http ditolak |
| `test_import_url_fetches_and_imports` | URL publik (mock) → fetch → impor draft |
| `test_import_skips_block_without_name` | Blok tanpa `name` di-skip, lanjut yang valid |
| `test_import_oversized_pack_rejected` | Pack > batas → ditolak |
| `test_parse_pack_multiple_skills` | Parser pisah banyak skill via delimiter |

---

### `tests/test_skill_scanner.py`

Test untuk `security/skill_scanner.py` (lapis scanner impor, terinspirasi skillspector). Yang kritis: skill berbahaya DITOLAK sebelum masuk DB, tanpa false-positive pada prosa, tanpa pernah crash.

| Test | Yang Diverifikasi |
|---|---|
| `test_clean_prose_passes` | Prosa biasa → `clean`, tak ada temuan |
| `test_exec_in_code_block_blocked` | `exec()` di blok kode → `reject` (AST kritis) |
| `test_subprocess_blocked` | `subprocess.run` → `reject` (skor ≥ HIGH) |
| `test_shell_exfil_pattern_blocked` | `curl … \| bash` → `reject` (pola eksfiltrasi) |
| `test_credential_path_flagged_or_blocked` | `~/.ssh/id_rsa` → minimal `flag` |
| `test_non_python_code_block_not_crash` | Blok bukan-Python → AST skip diam, tak crash |
| `test_open_write_is_medium_not_reject_alone` | `open(...,'w')` sendiri = medium, tak auto-reject |
| `test_never_raises_on_garbage` | Input biner/sampah → tak pernah raise |
| `test_import_rejects_high_risk_skill` | Integrasi: skill `exec()` tak masuk DB sama sekali |
| `test_import_clean_skill_succeeds` | Skill bersih tetap masuk draft (jalur normal utuh) |
| `test_import_flagged_skill_imported_with_label` | Risiko sedang → impor + tercatat di `flagged` |
| `test_import_url_rejects_high_risk` | Impor URL juga lewat scanner (defense-in-depth) |

---

### `tests/test_compaction.py`

Test untuk compaction headroom (`ContextCompactor.compact` + `SettingsStore.get/set_compaction_mode`). Yang kritis: opt-in, hemat tanpa kehilangan total, fail-safe ke truncation, idempoten.

| Test | Yang Diverifikasi |
|---|---|
| `test_no_compaction_when_history_fits` | History muat budget → tak ada LLM call, history utuh |
| `test_compaction_summarizes_old_keeps_recent` | Overflow → turn lama jadi 1 ringkasan, recent UTUH |
| `test_compaction_off_via_min_old_turns` | Turn lama < `min_old_turns` → tak diringkas |
| `test_summarizer_error_falls_back_to_original` | Summarizer error → history asli (truncation), tak crash |
| `test_empty_summary_falls_back` | Ringkasan kosong → tak ganti dengan blok kosong |
| `test_idempotent_does_not_recompact` | Blok yang sudah ringkasan tak diringkas lagi |
| `test_compaction_mode_default_off` | Default mode = `off` (aman) |
| `test_compaction_mode_roundtrip` | Set/get `local`/`cloud` |
| `test_compaction_mode_invalid_falls_back` | Nilai tak dikenal → fail-safe ke `off` |

---

### `tests/test_mcp.py`

Test integrasi MCP: client (SDK di-mock), wrapper `Tool`, SSRF, registry, izin wildcard.

| Test | Yang Diverifikasi |
|---|---|
| `test_mcp_tool_always_requires_approval` | **§1:** tool MCP selalu `requires_approval=True` |
| `test_mcp_tool_name_prefixed` | Nama `mcp__<server>__<tool>` |
| `test_mcp_tool_schema_from_server` | Schema dari server, deskripsi bertanda `[MCP:..]` |
| `test_mcp_tool_execute_strips_internal_fields` | Field `_*` tak diteruskan ke server |
| `test_http_transport_blocks_internal_host` | Remote ke host internal ditolak SSRF |
| `test_call_tool_failsafe_on_exception` | Exception → `{"error"}`, tak meledak |
| `test_list_tools_failsafe_returns_empty` | Discover gagal → `[]` |
| `test_extract_text_from_content_blocks` | Ekstraksi teks dari content blocks MCP |
| `test_add_and_list_server` | Registry CRUD: tambah & daftar server |
| `test_add_server_validates_transport` | Validasi transport/command/url |
| `test_toggle_and_delete_server` | Enable/disable & hapus server |
| `test_load_registers_discovered_tools` | `load_all` daftarkan tool ke `TOOL_REGISTRY` (prefix, approval) |
| `test_load_failsafe_on_bad_server` | Server gagal di-skip, startup tak jatuh |
| `test_load_idempotent_clears_old` | Reload tak menggandakan tool |
| `test_soul_wildcard_allows_mcp` | `mcp__*`/`mcp__server__*` mengizinkan; tanpa wildcard ditolak (opt-in) |

---

### `tests/test_skill_feedback.py`

Compounding **I2** (draft promotion) + **I3** (refine on correction) + prasyarat (revive skill terpakai). `SkillFeedback` jembatan antar-turn.

| Test | Yang Diverifikasi |
|---|---|
| `test_used_active_skill_revived_on_success` | Prasyarat: skill active dipakai+sukses → revive (use_count naik) |
| `test_draft_promoted_after_n_successes` | I2: draft dipakai-sukses N× → naik `active` |
| `test_draft_reset_on_correction` | I2: draft dikoreksi → counter reset, tetap draft |
| `test_active_skill_unaffected_by_draft_logic` | `record_draft_outcome` no-op pada skill active |
| `test_refine_applies_when_confident` | I3: koreksi + judge confident → konten ter-update, versi lama tersimpan |
| `test_refine_skipped_when_low_confidence` | I3: confidence rendah → konten TIDAK berubah (fail-safe) |
| `test_refine_disabled_by_config` | `refine_on_correction=False` → LLM tak dipanggil |
| `test_resolve_noop_when_no_pending` | Tanpa pending → no-op |
| `test_pending_marked_resolved_once` | Outcome diproses tepat sekali |

---

### `tests/test_calibration_auto.py`

Compounding **I4** — guarded auto-apply kalibrasi.

| Test | Yang Diverifikasi |
|---|---|
| `test_disabled_by_default` | `calibration_auto_apply=False` default → tak apply |
| `test_auto_apply_shifts_offset` | Aktif + under-provisioned → offset -1, `source='auto'` |
| `test_clamped_to_max_step` | Geser tak pernah > ±1 walau rekomendasi ekstrem |
| `test_insufficient_data_skips` | Sampel < min → skip (jangan menyetel dari noise) |
| `test_throttled_on_second_call` | Panggilan kedua langsung → throttled |
| `test_auto_apply_is_revertible` | Auto-apply dapat di-revert ke 0 |
| `test_timestamp_recorded` | Timestamp throttle tercatat |

---

### `tests/test_curator.py`

Compounding **I1** — Skill Curator (merge/dedup, anti data-loss, revert).

| Test | Yang Diverifikasi |
|---|---|
| `test_jaccard_identical_high` / `test_jaccard_disjoint_zero` | Similarity leksikal benar |
| `test_finds_similar_pair` | Pre-filter menemukan pasangan mirip |
| `test_no_pair_when_different` | Skill berbeda → tak ada kandidat |
| `test_merge_when_judge_confident` | Judge ≥4 → merge: winner active+konten gabungan, loser `merged` |
| `test_no_merge_when_judge_unsure` | Judge <4 → tak merge |
| `test_merge_preserves_loser_and_logs` | Loser tak dihapus; `curation_log` + `skill_versions` terisi |
| `test_revert_restores_loser` | Revert: loser → active, `merged_into` NULL |
| `test_revert_noop_when_no_merge` | Tanpa merge → no-op |
| `test_curation_throttled` | Pass kedua < interval → di-skip |

---

### `tests/test_user_model.py`

Compounding **I5** (opsional) — dialectic user model.

| Test | Yang Diverifikasi |
|---|---|
| `test_disabled_by_default` | Default nonaktif → profil kosong |
| `test_builds_profile_when_enabled` | Aktif → profil naratif dari L2 facts |
| `test_versioned_on_second_update` | Update kedua → versi 2, satu aktif |
| `test_throttled` | Panggilan kedua → throttled |
| `test_no_facts_skips` | Tanpa fakta → skip |
| `test_clear_removes_profile` | `clear()` hapus profil (privasi §1) |

---

### `tests/test_audit.py`

Test untuk `core/audit.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_log_and_finalize_roundtrip` | `log_decision()` → `finalize()` → event tersimpan lengkap di DB |
| `test_log_decision_defaults_actor_is_agent_true` | `actor_is_agent` default `1` tanpa perlu diberi eksplisit (§ Audit log format actor_is_agent) |
| `test_log_decision_stores_user_id_when_given` | `user_id` eksplisit tersimpan, query-able terpisah dari `session_id` |
| `test_log_decision_user_id_defaults_to_default_string` | Tanpa `user_id` eksplisit → `"default"`, bukan `NULL` (selaras `AgentConfig.user_id`) |
| `test_fallback_used_logged` | `fallback_used=True` tersimpan sebagai `1` |
| `test_fallback_not_used_defaults_zero` | Tanpa `fallback_used` → default `0` |
| `test_finalize_stores_evidence_json` | `finalize(evidence=...)` menyimpan snapshot JSON query-able (§ Evidence-Based Response) |
| `test_finalize_without_evidence_leaves_null` | `finalize()` tanpa argumen evidence → kolom tetap `NULL`, bukan `"null"`/dict kosong |
| `test_soul_upgrade_hit_logged` | `soul_upgrade_hit` tercatat di `dim_soul_upgrade_hit` |
| `test_correction_detected` | Sinyal koreksi (ID) → update `had_correction=1` |
| `test_correction_detected_english` | Sinyal koreksi (EN) juga terdeteksi (locale-neutral §1.5) |
| `test_no_correction_on_normal_query` | Query normal → `had_correction` tetap `0` |
| `test_correction_targets_most_recent_event` | Koreksi menandai event PALING TERAKHIR di sesi, bukan yang pertama |
| `test_calibration_report_empty` | Tanpa data → list kosong, tidak crash |
| `test_calibration_report_with_data` | Report mengelompokkan per `complexity_label` |
| `test_role_report_empty` | Tanpa data → list kosong, tidak crash (§ Runtime Evaluation Engine) |
| `test_role_report_groups_by_role` | `role_report` mengelompokkan per role (bukan per complexity_label) |
| `test_role_report_includes_correction_rate_per_role` | Correction rate dihitung per-role |
| `test_role_report_avg_human_feedback_null_when_none_given` | Role tanpa feedback → `avg_human_feedback` `NULL`, bukan `0` |
| `test_role_report_avg_human_feedback_computed_when_given` | `avg_human_feedback` dihitung hanya dari event yang PUNYA rating |
| `test_set_human_feedback_stores_rating` | `set_human_feedback()` menyimpan rating ke kolom `human_feedback` |
| `test_set_human_feedback_rejects_out_of_range` | Rating di luar 1-5 → `False`, tidak menulis apa pun |
| `test_set_human_feedback_unknown_event_returns_false` | `event_id` tak ditemukan → `False` |
| `test_all_correction_signals` | Semua `CORRECTION_SIGNALS` terdeteksi satu per satu |

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

Test untuk `security/` — Shield, Vault, ApprovalGate (HITL).

| Test | Yang Diverifikasi |
|---|---|
| `test_shield_passes_clean_input` | Input normal lolos shield |
| `test_shield_blocks_injection_english` | Prompt injection (EN) diblokir |
| `test_shield_blocks_injection_indonesian` | Prompt injection (ID) diblokir (locale-neutral §1.5) |
| `test_shield_normalizes_homoglyph` | Homoglyph (unicode lookalike) terdeteksi via NFKD |
| `test_shield_case_insensitive` | Deteksi tidak peduli huruf besar/kecil |
| `test_vault_returns_env_value` | `Vault.get()` baca dari `os.environ` |
| `test_vault_caches_value` | Nilai di-cache, tidak baca env berulang |
| `test_vault_missing_credential_raises` | Credential tidak ada → `ValueError` |
| `test_approval_approved_when_user_resolves_true` | `resolve(True)` → `request()` return `True`, `decision="approved"` |
| `test_approval_rejected_when_user_resolves_false` | `resolve(False)` → `request()` return `False`, `decision="rejected"` |
| `test_approval_timeout_fails_safe_deny` | KRITIS: timeout tanpa respons → fail-safe DENY |
| `test_approval_pending_cleared_after_decision` | Setelah resolve, `_pending` kosong (tak bocor memori) |
| `test_resolve_unknown_id_returns_false` | `resolve()` ID tak dikenal → `False`, tidak crash |
| `test_approval_id_stored_in_own_column` | Human Approval Pipeline (§ Prioritas 2): `approval_id` di kolom sendiri, query-able setelah `decision` berubah jadi final — bukan lagi tersirat di substring `pending:{id}` yang hilang |
| `test_approval_id_preserved_with_explicit_pre_generated_id` | `request(approval_id=...)` eksplisit (pola pre-generate `AgentLoop`) tersimpan utuh |
| `test_pending_list_scoped_by_session` | `pending_list(session_id)` hanya approval sesi itu |

---

### `tests/test_tools.py`

Test untuk `tools/`.

| Test | Yang Diverifikasi |
|---|---|
| `test_registry_has_all_27_tools` | Semua 27 tool terdaftar di registry |
| `test_file_read_returns_content` | `FileReadTool` baca file yang ada |
| `test_file_read_not_found` | File tidak ada → error dict (tidak crash) |
| `test_file_write_creates_file` | `FileWriteTool` tulis konten |
| `test_web_fetch_returns_content` | `WebFetchTool` fetch URL (mocked) |
| `test_code_run_requires_approval` | `CodeRunTool.requires_approval == True` |
| `test_file_write_requires_approval` | `FileWriteTool.requires_approval == True` |
| `test_file_read_no_approval_needed` | `FileReadTool.requires_approval == False` |
| `test_approval_called_for_destructive_tool` | Tool destruktif memanggil `ApprovalGate.request()` |
| `test_run_python_argv_enforces_security_flags` | argv **nyata** `run_python` memuat `--network none`, `--read-only`, non-root, `no-new-privileges`; semua mount `:ro` |
| `test_run_shell_argv_enforces_security_flags` | argv **nyata** `run_shell` menegakkan flag keamanan yang sama + workspace mount read-only |
| `test_run_python_fails_safe_when_docker_absent` | Docker absen → `SandboxUnavailable`, bukan eksekusi di host (keamanan #1) |
| `test_run_shell_fails_safe_when_docker_absent` | Sama untuk `run_shell` |
| `test_base_docker_args_contains_every_required_flag` | `_base_docker_args` (sumber argv tunggal) memuat semua `_REQUIRED_FLAGS` |
| `test_tool_exception_returns_error_not_crash` | Tool melempar exception → error dict anggun (§1.3), turn tak mati |
| `test_tool_timeout_returns_error` | Tool menggantung > `tool_timeout_sec` → error timeout |
| `test_tool_output_truncated_uniformly` | Output panjang dipotong ke `tool_max_output` apa pun tool-nya |
| `test_tool_missing_required_field_rejected_before_execute` | Field required hilang → error jelas, tool TIDAK dieksekusi |
| `test_tool_invocation_recorded_in_telemetry` | Eksekusi tercatat di `tool_invocations` (outcome=ok, latency) |
| `test_tool_failure_recorded_as_error_outcome` | Tool gagal → telemetri `outcome='error'` |
| `test_tool_audit_summary_aggregates` | `ToolAudit.summary()` agregasi total/errors/fail_rate per tool |
| `test_tool_audit_record_defaults_actor_is_agent_true` | `actor_is_agent` default `1` di `tool_invocations` (§ Audit log format actor_is_agent) |
| `test_tool_audit_record_stores_user_id` | `user_id` opsional tersimpan, query-able terpisah dari `session_id` |
| `test_read_many_reads_multiple_files` | `read_many` baca beberapa file workspace-safe sekaligus |
| `test_read_many_per_file_error_does_not_fail_others` | Satu file gagal → error per-file, lain tetap terbaca |
| `test_read_many_requires_list` | `paths` bukan list → error |
| `test_read_many_caps_batch_size` | > `MAX_FILES_PER_BATCH` → dipotong, `skipped` dilaporkan |
| `test_doc_write_requires_approval` | `doc_write.requires_approval == True` |
| `test_doc_write_rejects_unknown_format` | Format tak dikenal → error |
| `test_doc_write_markdown_string` | md dari string → file teks tertulis |
| `test_doc_write_docx_structured` | docx dari `{title,sections}` → .docx valid dibuka kembali |
| `test_doc_write_xlsx_rows` | xlsx dari `{headers,rows}` → spreadsheet baris benar |
| `test_doc_write_pptx_slides` | pptx dari `{title,slides}` → presentasi multi-slide |
| `test_doc_write_rejects_path_outside_workspace` | Path di luar workspace ditolak (keamanan #1) |
| `test_pm_dev_qa_have_doc_and_pdf_write_access` (parametrized pm/dev/qa) | § user request: QA harus bisa menulis test-case matrix Excel/laporan PDF — ketiga role punya `doc_write`+`pdf_write` di `[tools].allowed` soul.toml |
| `test_security_soul_unchanged_read_only` | Kontrol negatif: role `security` TETAP tanpa `file_write`/`doc_write`/`pdf_write` (read-only §17) |
| `test_ssrf_guard_blocks_loopback` | `_ssrf_guard` tolak `localhost`/`127.0.0.1`/`::1` |
| `test_ssrf_guard_blocks_cloud_metadata` | Tolak endpoint metadata cloud `169.254.169.254` (link-local) |
| `test_ssrf_guard_blocks_private_rfc1918` | Tolak alamat privat RFC1918 (10.x/192.168.x) |
| `test_ssrf_guard_allows_public_ip` | IP publik literal lolos guard |
| `test_ssrf_guard_blocks_dns_rebinding` | Domain yang resolve ke IP internal tetap diblokir (bukan hanya literal IP) |
| `test_web_fetch_rejects_internal_host` | `web_fetch` ke host internal ditolak SEBELUM request keluar (tanpa approval) |
| `test_web_fetch_rejects_non_http_scheme` | Scheme selain http/https (mis. `file://`) ditolak |
| `test_http_request_rejects_internal_host` | `http_request` diblokir SSRF walau butuh approval |

---

### `tests/test_tools_batch3.py`

Test tool batch 3: git (sandbox), `todo_write` (DB), `pdf_write` (reportlab).

| Test | Yang Diverifikasi |
|---|---|
| `test_git_tools_are_read_only` | git_status/diff/log `requires_approval=False` |
| `test_git_status_runs_in_sandbox_not_host` | git_status lewat `DockerSandbox.run_shell` (`git -C /work …`), bukan host |
| `test_git_log_count_clamped` | `count` dijepit ke ≤ 50 |
| `test_git_diff_path_is_quoted` | `path` di-`shlex.quote` → cegah injeksi opsi/perintah |
| `test_git_status_fails_safe_without_docker` | Docker absen → error anggun, bukan eksekusi host |
| `test_git_status_reports_non_repo` | Workspace bukan repo git → pesan jelas |
| `test_todo_write_persists_list` | `todo_write` simpan daftar per sesi dengan status |
| `test_todo_write_replaces_previous_snapshot` | Panggilan kedua mengganti daftar (snapshot) |
| `test_todo_write_rejects_bad_status` | Status tak valid → error, tidak menulis |
| `test_todo_write_requires_session` | Tanpa `_session_id` → error |
| `test_todo_write_no_approval` | `todo_write.requires_approval=False` (tabel internal) |
| `test_pdf_write_requires_approval` | `pdf_write.requires_approval=True` |
| `test_pdf_write_produces_pdf` | Menghasilkan PDF nyata (header `%PDF`) |
| `test_pdf_write_rejects_outside_workspace` | Path di luar workspace ditolak |
| `test_pdf_write_rejects_bad_content` | content bukan objek → error |

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
| `test_under_provisioned_suggests_negative_offset` | Under → `offset_delta == -1` |
| `test_over_provisioned_suggests_positive_offset` | Over → `offset_delta == +1` |
| `test_summary_net_offset_clamped_to_one_step` | Banyak saran searah → `net_offset_delta` dijepit ke satu langkah |
| `test_summary_net_offset_zero_when_conflicting` | Saran berlawanan → `net_offset_delta == 0` |
| `test_offset_defaults_to_zero` | `CalibrationStore.get_offset()` default 0 |
| `test_apply_shifts_offset_and_logs_audit` | apply geser offset + tulis `app_settings` + baris audit aktif |
| `test_apply_clamped_to_bounds` | Offset dijepit ke `[OFFSET_MIN, OFFSET_MAX]` |
| `test_revert_restores_previous_offset` | revert kembalikan offset + baris `source='revert'` |
| `test_revert_noop_when_no_history` | revert tanpa riwayat → no-op, tak crash |
| `test_only_one_active_row_after_multiple_applies` | Invarian: tepat satu baris `active=1` |
| `test_corrupt_offset_value_fails_safe_to_zero` | Nilai korup → `get_offset()` fail-safe ke 0 |
| `test_router_negative_offset_upgrades_sooner` | Offset negatif → router naik tier ≥ |
| `test_router_positive_offset_stays_cheaper` | Offset positif → router bertahan tier ≤ |

---

### `tests/test_compactor.py`

Test untuk `core/compactor.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_history_trimmed_when_over_budget` | History lama dipotong jika melebihi token budget |
| `test_system_prompt_always_included` | System prompt selalu ada di messages[0] |
| `test_memory_sections_in_system_prompt` | L1/L2/L3/L4 muncul di system prompt jika ada |
| `test_estimate_context_tokens_sums_all_messages` | `estimate_context_tokens` jumlah token semua message |
| `test_estimate_context_tokens_empty_and_missing_content` | Pesan kosong/tanpa content → 0, tak crash |
| `test_estimate_context_tokens_matches_build_output` | Estimasi konsisten dengan hasil `build()` |

---

### `tests/test_web.py`

Smoke test untuk endpoints Web UI.

| Test | Yang Diverifikasi |
|---|---|
| `test_metrics_renders_empty` | `GET /metrics` render tanpa data (tidak crash) |
| `test_index_renders` | `GET /` return 200 |
| `test_health_endpoint` | `GET /health` → JSON status + cek DB (monitoring self-hosted) |
| `test_settings_renders_with_compaction_control` | `/settings` render 200 + dropdown compaction (default off) |
| `test_settings_save_compaction_mode` | `POST /settings` simpan mode compaction; round-trip di GET |
| `test_index_lists_new_roles` | Sidebar + chip memuat `data` & `security` |
| `test_index_unknown_role_falls_back` | `?role=` tak dikenal → 200 (fallback role pertama) |
| `test_approve_requires_valid_params` | `POST /approve` tanpa params → `ok=False`, tidak crash |
| `test_approvals_empty` | `GET /approvals` return list kosong |
| `test_metrics_shows_active_offset` | `/metrics` menampilkan offset threshold aktif |
| `test_calibration_apply_then_revert_roundtrip` | Apply -1 lalu revert tercermin di `/metrics` |
| `test_calibration_apply_zero_delta_is_noop` | `delta=0` → offset tetap 0 |
| `test_skills_page_renders_empty` | `GET /skills` tanpa skill → 200 + pesan kosong |
| `test_skills_page_shows_seeded_skill` | Skill di DB muncul di tabel `/skills` |
| `test_skills_page_shows_crystallization_attempt` | Percobaan kristalisasi tampil di `/skills` |
| `test_conversations_page_renders_empty` | `/conversations` tanpa arsip → 200 + pesan kosong |
| `test_conversations_page_shows_archived_run` | Percakapan tersimpan tampil dengan pattern/peserta/transkrip |
| `test_router_page_renders_tiers` | `/router` menampilkan 5 tier + dropdown + tanda default |
| `test_router_save_then_reflected` | Simpan peta → "Peta kustom aktif"; reset → "memakai peta default" |
| `test_converse_interject_unknown_session` | `/converse/interject` sesi tak aktif → `ok=False`, tak crash |
| `test_converse_stop_unknown_session` | `/converse/stop` sesi tak aktif → `ok=False`, tak crash |
| `test_converse_interject_and_stop_reach_live_control` | Interject & stop mencapai `ConversationControl` di registry sesi |
| `test_converse_stream_emits_named_frames` | `/converse/stream` (orchestrator di-mock) → frame SSE `turn`/`token`/`conversation_end`/`done` |
| `test_converse_stream_rejects_unknown_pattern` | Pattern tak dikenal → frame `error`, bukan 500 |
| `test_activity_page_renders_empty` | `/activity` tanpa peristiwa → 200 + filter peran |
| `test_activity_page_shows_seeded_events` | Peristiwa observability muncul di linimasa |
| `test_activity_page_role_filter` | `?role=` fokus peran; role tak dikenal tak crash |
| `test_activity_shows_open_blocker_and_resolve` | Blocker terbuka tampil di banner; `/blockers/resolve` menutup |
| `test_autopilots_page_renders_empty` | `/autopilots` → 200 + form + catatan keamanan |
| `test_autopilots_create_then_listed` | Buat autopilot → muncul; role tak dikenal ditolak |
| `test_autopilots_toggle_and_delete` | Toggle menjeda, delete menghapus |
| `test_mcp_page_renders_empty` | `/mcp` → 200 + form + catatan keamanan |
| `test_mcp_add_stdio_server` | Tambah server stdio → muncul (discover fail-safe) |
| `test_mcp_add_http_rejects_internal` | Server http internal: tersimpan tapi tool tak ter-discover (SSRF) |
| `test_mcp_toggle_and_delete` | Toggle & hapus server MCP |
| `test_skills_export_returns_markdown` | `/skills/export` → berkas Markdown (attachment) |
| `test_skills_import_lands_as_draft` | `/skills/import` → skill draft, muncul di `/skills` |
| `test_skills_import_blocks_injection` | Pack berpola injeksi ditolak, tak muncul |
| `test_skills_page_shows_curation` | Jejak merge (I1) tampil + tombol Batalkan |
| `test_skills_revert_merge_endpoint` | `/skills/revert-merge` kembalikan loser ke active |
| `test_metrics_shows_auto_apply_badge` | `/metrics` tampilkan badge auto-tune (I4) |

---

### `tests/test_settings.py`

Test untuk fitur pilih model: `SettingsStore`, provider Gemini, override routing.

| Test | Yang Diverifikasi |
|---|---|
| `test_override_none_by_default` | Tanpa setting → mode otomatis (`None`) |
| `test_set_and_get_override` | Set override → kebaca sebagai `(provider, model)` |
| `test_clear_override_returns_to_auto` | Set lalu hapus → kembali ke otomatis |
| `test_partial_override_is_not_active` | Hanya provider tanpa model → bukan override valid |
| `test_override_upsert_overwrites` | Set dua kali → nilai terakhir menang |
| `test_gemini_provider_dispatch` | `_stream_one` mengarahkan `gemini` ke `_gemini()` |
| `test_gemini_health_check_assumes_up` | Gemini diasumsikan up (seperti anthropic) |
| `test_gemini_parses_sse_stream` | `_gemini` mem-parse SSE Google AI Studio → text + usage |
| `test_gemini_sends_tools_as_function_declarations` | Regresi bug halusinasi pdf_write: `tools` diteruskan ke `_gemini()` & dikonversi ke `functionDeclarations` |
| `test_gemini_parses_function_call_response` | Response `functionCall` Gemini → `LLMChunk(type="tool_call")` dengan `tool_input` terisi dari `args` |
| `test_override_changes_route_in_agent_loop` | Override aktif → provider/model ke LLM dipaksa sesuai pilihan |
| `test_no_override_uses_router` | Tanpa override → router tetap memilih (query pendek → lokal) |
| `test_usage_event_carries_token_budget` | Event `usage` memuat `context_tokens` & `max_context_tokens` (meter budget §1.4) |

---

### `tests/test_question.py`

Test untuk `security/question.py` (QuestionGate) + integrasi `ask_user` lewat AgentLoop.

| Test | Yang Diverifikasi |
|---|---|
| `test_ask_resolved_returns_answer` | `ask()` menunggu; `resolve_by_session()` memberi jawaban |
| `test_ask_timeout_returns_no_answer` | Tanpa jawaban dalam batas waktu → `NO_ANSWER` (fail-soft) |
| `test_resolve_by_id` | `resolve(question_id)` bekerja; `pending_list` mengekspos id |
| `test_resolve_unknown_session_returns_false` | Sesi tanpa pertanyaan → `False`, tidak crash |
| `test_resolve_unknown_id_returns_false` | id tak dikenal → `False` |
| `test_pending_list_filters_by_session` | `pending_list` menyaring per session |
| `test_agent_loop_ask_user_uses_gate` | `_execute_tool('ask_user')` lewat gate (bukan stub) → jawaban user |
| `test_agent_loop_ask_user_empty_question` | `ask_user` tanpa question → error, tidak menggantung |

---

### `tests/test_thinking.py`

Test untuk `ThinkTagSplitter` + parsing reasoning per provider (`LLMChunk(type="thinking")`).

| Test | Yang Diverifikasi |
|---|---|
| `test_plain_text_no_think` | Teks tanpa tag → semua `text` |
| `test_think_then_answer_single_chunk` | `<think>x</think>jawaban` → thinking + text terpisah |
| `test_tag_split_across_chunks` | Tag terpotong antar-chunk tidak bocor sebagai teks |
| `test_close_tag_split_across_chunks` | Tag penutup terpotong tetap dikenali |
| `test_unclosed_think_flushed` | `<think>` tak tertutup → di-flush sebagai thinking |
| `test_text_before_think` | Teks sebelum `<think>` tetap utuh |
| `test_no_think_with_angle_bracket` | `<` yang bukan tag think tidak rusak |
| `test_ollama_inline_think_split` | Ollama `<think>` inline → chunk thinking |
| `test_ollama_thinking_field` | Ollama field `message.thinking` → chunk thinking |
| `test_anthropic_thinking_delta` | Anthropic `thinking_delta` → chunk thinking |
| `test_gemini_thought_part` | Gemini `parts[].thought=true` → chunk thinking |

---

### `tests/test_logging.py`

Test untuk `infra/logging.py` (structlog JSON + secret-scrubbing).

| Test | Yang Diverifikasi |
|---|---|
| `test_setup_logging_idempotent` | `setup_logging()` aman dipanggil berkali-kali |
| `test_log_is_callable_logger` | `log` punya method level standar (debug/info/warning/error) |
| `test_setup_logging_renders_json` | Output log = JSON satu baris dengan level + event + timestamp |
| `test_scrub_redacts_sensitive_key_names` | Field `api_key`/`token`/`secret`/`password` di-redact penuh |
| `test_scrub_redacts_secret_patterns_in_values` | Nilai berpola secret (sk-/bearer/gh token) di-redact |
| `test_scrub_leaves_normal_values` | Teks biasa tak diubah (tanpa false-positive) |
| `test_scrub_fail_soft_on_bad_input` | Input aneh tak meledak (logging tak boleh gagal karena scrub) |
| `test_setup_logging_scrubs_in_pipeline` | End-to-end: secret tak muncul di output JSON |

---

### `tests/test_database.py`

Test untuk `infra/database.py` — `DatabaseManager._ensure_columns()` (auto-tambal kolom
di tabel lama). Regresi nyata: DB dibuat sebelum kolom I1 ditambahkan ke migration tak
pernah dapat kolom baru (`CREATE TABLE IF NOT EXISTS` no-op pada tabel existing) → 500 di
`/skills` (`no such column: status`).

| Test | Yang Diverifikasi |
|---|---|
| `test_ensure_columns_patches_missing_curation_log_columns` | DB skema lama → `curation_log.status`/`merged_content` muncul setelah `run_migration` |
| `test_ensure_columns_patches_missing_skills_columns` | `skills.merged_into`/`version`/`draft_success_count` ditambal dengan default benar |
| `test_ensure_columns_preserves_existing_data` | Tambal kolom TIDAK mengubah/menghapus data lama |
| `test_ensure_columns_idempotent_on_second_run` | Jalan dua kali (restart berulang) tak error kolom duplikat |
| `test_ensure_columns_noop_on_fresh_db` | DB baru (skema lengkap dari CREATE TABLE) — tak berefek |

---

### `tests/test_i18n.py`

Test untuk `infra/i18n.py` (`t()`/`translator()`) + `SettingsStore.get/set_ui_locale`.
Locale UI murni untuk teks statis (nav/tombol) — TIDAK menyentuh bahasa respons agent (§1.5).

| Test | Yang Diverifikasi |
|---|---|
| `test_default_locale_is_english` | `DEFAULT_LOCALE="en"`, `LOCALES` berisi en+id |
| `test_t_returns_english_by_default` / `test_t_returns_indonesian_when_requested` | `t()` mengambil locale yang diminta |
| `test_t_unknown_locale_falls_back_to_english` | Locale tak dikenal → fallback English |
| `test_t_unknown_key_returns_key_itself` | Key hilang → fail-safe kembalikan key (tak exception) |
| `test_t_formats_placeholders` / `test_t_bad_format_kwargs_falls_back_to_unformatted_text` | `str.format` placeholder + fail-safe kwargs salah |
| `test_translator_closure_binds_locale` / `test_translator_unknown_locale_normalizes_to_default` | Closure `translator()` untuk Jinja2 context |
| `test_every_string_has_both_locales` | Setiap entri `STRINGS` wajib punya en+id (cegah UI kosong saat toggle) |
| `test_ui_locale_default_english` / `test_ui_locale_roundtrip` / `test_ui_locale_invalid_falls_back_to_english` / `test_ui_locale_none_resets_to_english` | `SettingsStore` persist locale, fail-safe ke English |

---

### `tests/test_auth.py`

Test untuk `security/auth.py` (unit, tanpa HTTP) — signing/verifikasi token sesi, CSRF
token, deteksi path publik. Security-critical: signature harus tolak token dipalsukan.

| Test | Yang Diverifikasi |
|---|---|
| `test_valid_token_verifies` / `test_wrong_secret_rejected` | Token sah lolos, secret salah ditolak |
| `test_none_token_rejected` / `test_empty_token_rejected` / `test_malformed_token_no_dot_rejected` / `test_non_numeric_timestamp_rejected` | Input aneh ditolak, tak exception |
| `test_tampered_timestamp_rejected` / `test_tampered_signature_rejected` | Modifikasi token → signature tak cocok, ditolak |
| `test_expired_token_rejected` / `test_future_timestamp_rejected` | Token di luar `SESSION_MAX_AGE_SEC` (masa lalu/masa depan) ditolak |
| `test_login_token_matches` / `test_login_token_mismatch` | Verifikasi password login constant-time |
| `test_csrf_token_is_random_and_url_safe` | Tiap token CSRF unik |
| `test_public_paths_allowed_without_session` / `test_protected_paths_not_public` | `/health`, `/login`, `/static/*` publik; sisanya tidak |

---

### `tests/test_auth_web.py`

Test end-to-end untuk `auth_and_csrf_middleware` di `web/main.py` (bukan unit
`security/auth.py`) — perilaku HTTP nyata via `TestClient`. Dua fixture: `client_no_auth`
(auth nonaktif, default) dan `client_auth` (`OPENCLAWN_AUTH_TOKEN` diset).

| Test | Yang Diverifikasi |
|---|---|
| `test_no_auth_root_accessible_without_session` | Auth nonaktif → `/` tetap 200 tanpa sesi (tak ada regresi perilaku lama) |
| `test_no_auth_health_reports_auth_disabled` | `/health.auth_enabled == False` saat token kosong |
| `test_auth_enabled_redirects_unauthenticated_get_to_login` | GET tanpa sesi valid → 303 ke `/login` |
| `test_auth_enabled_unauthenticated_post_returns_401_json` | POST tanpa sesi valid → 401 JSON (bukan redirect, agar fetch API tahu) |
| `test_health_and_login_reachable_without_session` / `test_static_reachable_without_session` | Path publik tetap 200 tanpa sesi walau auth aktif |
| `test_login_wrong_token_rejected` | Password salah → redirect `/login?error=true`, tak set cookie sesi |
| `test_login_correct_token_sets_cookies_and_grants_access` | Password benar → cookie sesi+CSRF ter-set, halaman utama lolos |
| `test_login_rejects_open_redirect_via_next` | `?next=` ke domain eksternal dinetralkan ke `/` |
| `test_csrf_missing_token_rejected_after_login` / `test_csrf_valid_token_allows_post` | POST form tanpa token CSRF ditolak (403); dengan token cocok diterima |
| `test_csrf_exempt_paths_bypass_check` | `/answer` (endpoint fetch JS) tak butuh token CSRF |
| `test_logout_clears_session` / `test_logout_without_csrf_rejected` | Logout hapus cookie via form ber-CSRF; tanpa CSRF ditolak sama seperti form lain |

---

### `tests/test_rate_limit.py`

Test untuk `security/rate_limit.py` (`RateLimiter`) — sliding window in-memory.

| Test | Yang Diverifikasi |
|---|---|
| `test_allows_up_to_max_requests` / `test_blocks_after_max_requests` | Kuota per window ditegakkan |
| `test_different_keys_independent` | Key berbeda (mis. sesi berbeda) tak saling memengaruhi kuota |
| `test_window_expiry_allows_again` | Hit di luar window tak lagi dihitung |
| `test_rejected_hit_not_counted` | Request yang DITOLAK tak ikut disimpan (retry setelah window lewat tak terhambat) |
| `test_remaining_reflects_usage` / `test_remaining_never_negative` | `remaining()` akurat, tak pernah negatif |

---

### `tests/test_file_download.py`

Test untuk fitur download file yang ditulis agent. Dua bagian: `AgentEvent(type="file_created")`
di `core/agent_loop.py` (unit, LLM di-mock via `_fake_stream_calling_tool`), dan endpoint
`GET /workspace/download` di `web/main.py` (via `TestClient`). **Penting:** tool file
(`tools/file_ops.py`) memakai `CONFIG` singleton module-level, bukan config instance yang
dipassing ke `AgentLoop` — test WAJIB patch `tools.file_ops.CONFIG` (helper `_set_workspace`,
pola sama `tests/test_tools.py`) agar tidak menulis file sungguhan ke root proyek.

| Test | Yang Diverifikasi |
|---|---|
| `test_file_created_event_emitted_on_successful_write` | `file_write` sukses (approval granted) → event `file_created` dengan path resolved di workspace sementara |
| `test_file_created_event_not_emitted_when_approval_denied` | Approval ditolak → file tak ditulis → tak ada event (anti false-positive) |
| `test_file_created_event_not_emitted_for_readonly_tools` | Tool baca (`grep`) tak pernah memicu `file_created` |
| `test_file_created_not_emitted_on_workspace_violation` | Path di luar workspace → `file_write` gagal (`error`, bukan `ok`) → tak ada event |
| `test_download_existing_file_in_workspace` / `test_download_nested_path_in_workspace` | File (termasuk di subfolder) di dalam workspace ter-download dengan isi benar |
| `test_download_missing_file_returns_404` | File tak ada → 404 |
| `test_download_path_traversal_returns_404_not_file_content` | `../` keluar workspace → 404, isi file di luar workspace TIDAK bocor ke response |
| `test_download_directory_returns_404_not_error` | Path menunjuk direktori → 404 anggun, bukan exception |
| `test_approval_status_event_emitted_before_blocking_request` | Event `status`/`approval` muncul dengan `approval_id` valid SEBELUM `ApprovalGate.request()` selesai; ID yang sama diteruskan ke `request()` |
| `test_no_approval_event_for_readonly_tools` | Tool tanpa `requires_approval` (`grep`) tak pernah memicu event `approval` |
| `test_no_approval_event_in_autopilot_mode` | Mode autopilot (proposal, bukan Future hidup) → tak ada event `approval` |

---

### `tests/test_agent_tool_loop.py`

Test perbaikan tool loop di `core/agent_loop.py` (§ user report: agent menulis file berulang untuk task simpel). LLM di-mock via `stream_with_fallback` (pola sama `test_guardrails.py`); file-write diarahkan ke `tmp_path` lewat `workspace_override` agar aman.

| Test | Yang Diverifikasi |
|---|---|
| `test_format_result_file_write_success_is_terminal` | `_format_tool_result` untuk file_write sukses → teks "SUCCESS ... do not write it again" (sinyal terminal untuk model lokal) |
| `test_format_result_error_is_clear` | Hasil error → `ERROR: <pesan>` |
| `test_format_result_generic_ok` | Hasil ok lain → `SUCCESS: k=v...` |
| `test_format_result_non_dict_falls_back` | Hasil bukan dict → `str(result)` |
| `test_assistant_tool_call_written_back_to_messages` | Setelah tool jalan, `messages` memuat giliran `assistant` dengan `tool_calls` + hasil `role="tool"`; model dipanggil tepat 2× (tool → selesai), tidak looping |
| `test_same_path_write_twice_triggers_loop_stop` | Menulis path yang SAMA dua kali berturut-turut → `AgentEvent(status, loop_stopped)`, bukan menulis tanpa batas |

---

### `tests/test_session_history.py`

Test persistensi riwayat percakapan per-sesi (§ user report: agent seolah tak baca chat sebelumnya, bahkan di sesi yang sama). `MemoryManager.load_turns`/`append_turn` + reload di `AgentLoop._run()`.

| Test | Yang Diverifikasi |
|---|---|
| `test_append_and_load_turns_roundtrip` | Simpan lalu muat → urut lama→baru, isi utuh |
| `test_load_turns_isolated_per_session` | Giliran satu sesi tak bocor ke sesi lain (filter `session_id`) |
| `test_load_turns_caps_at_limit_keeping_newest` | `limit` mempertahankan giliran TERBARU, tetap urut lama→baru |
| `test_append_turn_skips_empty` | Konten kosong tak disimpan |
| `test_second_agentloop_sees_prior_turn` | AgentLoop BARU (request berikutnya, session sama) melihat user+assistant turn sebelumnya di `messages`; pesan baru tetap giliran user terakhir |
| `test_persist_history_false_does_not_load_or_store` | Multi-agent (`persist_history=False`) tak memuat & tak menyimpan `session_turns` |

---

### `tests/test_heartbeat.py`

Test SSE heartbeat (§ user report: "Server not responding" & diam sebelum selesai). `_with_heartbeat` menyisipkan komentar `: ping` selama jeda — koneksi TIDAK putus, tak ada yang perlu reconnect.

| Test | Yang Diverifikasi |
|---|---|
| `test_heartbeat_fires_during_quiet_gap` | Jeda > interval → ≥1 `: ping`, data frame tetap utuh & urut |
| `test_no_heartbeat_when_source_is_fast` | Sumber tanpa jeda → tak ada ping (stream normal tak terkotori) |
| `test_source_exception_propagates` | Error sumber diteruskan ke caller, tak ditelan |
| `test_empty_source_completes_cleanly` | Sumber kosong → selesai tanpa ping menggantung |

---

### `tests/test_trust_mode.py`

Test trust mode per-sesi (§ user request otonomi: kurangi approval yang tak perlu). Tool yang butuh approval tetap DIEKSEKUSI, hanya melewati klik manusia — `code_run` selalu dikecualikan (CLAUDE.md §1).

| Test | Yang Diverifikasi |
|---|---|
| `test_shell_run_no_longer_requires_approval` | `ShellRunTool.requires_approval` sekarang `False` (sandbox = pertahanan, bukan approval) |
| `test_code_run_still_requires_approval` | Kontrol negatif: `CodeRunTool.requires_approval` tetap `True` |
| `test_code_run_is_trust_mode_exempt` | `"code_run"` ada di `_TRUST_MODE_EXEMPT` |
| `test_trust_mode_bypasses_approval_and_executes_for_real` | Trust mode aktif → `file_write` benar-benar menulis file (bukan cuma diloloskan), lewat `auto_approve`; tercatat `decision="auto:trust_mode"` |
| `test_trust_mode_never_bypasses_code_run` | `code_run` + `trust_mode=True` + `bypass_approval=True` tetap lewat `approval.request()` normal, `auto_approve` tak pernah dipanggil |
| `test_bypass_approval_false_uses_normal_request` | `bypass_approval=False` → jalur `request()` biasa, `auto_approve` tak dipanggil |
| `test_autopilot_wins_over_trust_mode` | Autopilot tetap PROPOSAL walau `trust_mode=True` — `auto_approve` tak dipanggil |
| `test_auto_approve_records_trust_decision_and_returns_true` | `ApprovalGate.auto_approve` mencatat `decision="auto:trust_mode"` & return `True` |
| `test_policy_forced_approval_not_bypassable_even_if_caller_passes_bypass_true` | Defense-in-depth (§ Policy Engine, TODO.md Prioritas 3): trust mode TIDAK bisa melewati approval yang dipaksa policy, bahkan bila caller keliru meneruskan `bypass_approval=True` |
| `test_policy_deny_blocks_before_approval_entirely` | Policy `deny_if` menolak SEBELUM approval sempat dipanggil sama sekali |

---

### `tests/test_policy_engine.py`

Test untuk `security/policy_engine.py` — Policy Engine sederhana (§ TODO.md § Prioritas 3). Kondisi nested dict/TOML, bukan DSL string/`eval()`.

| Test | Yang Diverifikasi |
|---|---|
| `test_no_policy_configured_allows_by_default` | Tool tanpa section `[policy.<tool>]` → `allow` (perilaku lama tak berubah) |
| `test_deny_if_prefix_match_denies` / `_no_match_allows` | Operator `prefix` pada `deny_if` |
| `test_deny_if_gt_denies_when_exceeded` / `_allows_when_under` | Operator `gt` (numerik) |
| `test_approval_required_if_condition_met` / `_not_met` | `approval_required_if` dengan operator `not_prefix` |
| `test_deny_takes_priority_over_approval_required` | `deny_if` menang atas `approval_required_if` bila keduanya match (fail-safe, CLAUDE.md §1) |
| `test_contains_operator` | Operator `contains` |
| `test_gte_lte_operators` | Operator `gte`/`lte` |
| `test_lt_operator` | Operator `lt` |
| `test_eq_operator` | Operator `eq` |
| `test_missing_field_in_tool_input_does_not_match_condition` | Field kondisi tak ada di `tool_input` → tidak match, bukan crash |
| `test_unknown_operator_is_ignored_fail_safe` | Operator tak dikenal (typo config) → diabaikan, tidak menjatuhkan tool loop |
| `test_multiple_deny_conditions_any_match_denies` | Beberapa kondisi `deny_if` — OR semantics (satu match sudah cukup) |
| `test_always_operator_matches_without_field_in_tool_input` | Operator `always` (dipakai `infra/manifest.py`) match tanpa field di `tool_input` |
| `test_policy_decision_is_dataclass_with_reason` | `PolicyDecision` dataclass dasar |

---

### `tests/test_manifest.py`

Test untuk `infra/manifest.py` — `clawn.yaml` sebagai lapisan deklaratif di atas `soul.toml` (§ TODO.md § Prioritas 3).

| Test | Yang Diverifikasi |
|---|---|
| `test_load_manifest_parses_team_roles` | `load_manifest` parse `team.<role>.policy` dengan benar |
| `test_load_manifest_missing_file_raises_manifest_error` | File manifest tak ada → `ManifestError` |
| `test_load_manifest_invalid_yaml_raises_manifest_error` | YAML tak valid → `ManifestError`, bukan exception PyYAML mentah |
| `test_load_manifest_missing_team_key_raises_manifest_error` | Manifest tanpa key root `team` → `ManifestError` |
| `test_generate_policy_toml_block_renders_deny_if` | Render `deny_if` jadi blok `[policy.<tool>]` TOML |
| `test_generate_policy_toml_block_renders_approval_required` | Render `approval_required_if` (termasuk operator `always`) |
| `test_generate_policy_toml_block_handles_numeric_value` | Nilai numerik ditulis TANPA quote (beda dari string) |
| `test_generate_policy_toml_block_empty_policy_returns_empty_string` | Dict kosong → string kosong |
| `test_apply_manifest_appends_policy_to_soul_without_existing_policy` | `soul.toml` tanpa `[policy]` sama sekali → blok baru ditambahkan, `system_prompt` multi-baris & section lain tetap byte-identik |
| `test_apply_manifest_replaces_existing_policy_section` | `soul.toml` SUDAH punya `[policy.*]` → diganti bersih (tidak menumpuk duplikat), section lain utuh |
| `test_apply_manifest_role_not_in_manifest_leaves_soul_untouched` | Role tak disebut manifest → `soul.toml`-nya tak disentuh sama sekali (opt-in per-role) |
| `test_apply_manifest_missing_soul_file_raises_manifest_error` | Role disebut manifest tapi `soul.toml`-nya tak ada → `ManifestError`, bukan membuat file baru diam-diam |
| `test_apply_manifest_role_without_policy_key_is_noop_for_that_role` | Role ada di manifest tapi tanpa key `policy` (mis. hanya `model`) → no-op untuk role itu |

---

### `tests/test_set_workdir.py`

Test pindah direktori kerja dinamis lewat chat (§ user request: "pindah direktori secara dinamis" — sebelumnya folder kerja HANYA bisa diubah lewat field UI, tak ada cara mengubahnya dari dalam percakapan).

| Test | Yang Diverifikasi |
|---|---|
| `test_session_workspace_get_set_roundtrip` | `SessionWorkspaceStore.set`/`get` roundtrip |
| `test_session_workspace_upsert_overwrites` | `set()` kedua menimpa nilai lama (UPSERT, bukan duplikat baris) |
| `test_session_workspace_isolated_per_session` | Folder sesi A tak bocor ke sesi B |
| `test_set_workdir_success_sets_contextvar_and_db` | Sukses → `CURRENT_WORKSPACE_ROOT` berubah LANGSUNG (turn ini ikut pindah) + tersimpan ke `session_workspace` (turn berikutnya) |
| `test_set_workdir_missing_path_errors` | `path` kosong → error, bukan crash |
| `test_set_workdir_nonexistent_folder_errors` | Folder tak ada → error; DB TIDAK berubah (fail-closed) |
| `test_set_workdir_missing_session_id_errors` | `_session_id` absen (dipanggil di luar AgentLoop) → error anggun |
| `test_set_workdir_registered_and_no_approval` | Terdaftar di `TOOL_REGISTRY`, `requires_approval=False` |
| `test_workdir_change_persists_to_next_agentloop` | AgentLoop TURN 1 panggil `set_workdir` → AgentLoop BARU turn 2 (sesi sama, tanpa form workdir) otomatis pakai folder baru — `file_read` di folder itu sukses |
| `test_explicit_workspace_override_wins_over_saved_workdir` | Form UI diisi eksplisit di request ini → menang atas `session_workspace` tersimpan |

---

### `tests/test_chat_sessions.py`

Test sidebar riwayat chat (§ user report: chat selalu ke-reset, tak ada cara buka chat baru/lanjutkan/hapus riwayat). `_post_turn` diuji LANGSUNG (`await agent._post_turn(...)`), bukan lewat `agent.run()` penuh — ia dijadwalkan sebagai background task terpisah (`asyncio.create_task`), pola sama `test_memory_wiring.py`.

| Test | Yang Diverifikasi |
|---|---|
| `test_truncate_short_message_unchanged` | Pesan pendek (≤ head+tail kata) dikirim utuh ke LLM judul |
| `test_truncate_long_message_keeps_head_and_tail` | Pesan panjang dipotong jadi `head ... tail`, jauh lebih pendek dari asli |
| `test_truncate_exact_boundary_unchanged` | Persis di batas head+tail kata → tidak dipotong |
| `test_ensure_created_idempotent` | Panggilan kedua tak duplikat baris |
| `test_ensure_created_does_not_overwrite_existing_title` | `INSERT OR IGNORE` tak menimpa title yang sudah ada |
| `test_set_title_strips_quotes_and_truncates` | Tanda kutip pembungkus dibuang; judul dipotong ke `MAX_TITLE_CHARS` |
| `test_has_title_false_until_set` | Gate generate-judul akurat sebelum/sesudah `set_title` |
| `test_touch_updates_timestamp` | `touch()` memperbarui `updated_at` (urutan sidebar terbaru dulu) |
| `test_list_active_excludes_deleted` | Sesi yang di-soft-delete tak muncul di `list_active` |
| `test_soft_delete_hard_deletes_turns_and_workspace` | `soft_delete` menghapus FISIK `session_turns`+`session_workspace`, metadata `chat_sessions` tetap ada (`deleted_at` terisi) |
| `test_list_active_respects_limit` | Parameter `limit` dihormati |
| `test_title_generated_on_first_turn` | Turn pertama → judul di-generate dari LLM lokal & tersimpan |
| `test_title_not_regenerated_on_second_turn` | Turn kedua (sudah punya judul) → LLM judul TAK dipanggil lagi (hemat token) |
| `test_title_generation_failure_does_not_crash_turn` | LLM judul gagal (exception) → `_post_turn` tetap selesai normal (fail-safe §1.3) |
| `test_multi_agent_does_not_generate_title` | `persist_history=False` → `chat_sessions` tak tersentuh sama sekali |
| `test_list_chat_sessions_empty_initially` | `GET /chat-sessions` kosong sebelum ada sesi |
| `test_list_chat_sessions_fallback_title_new_chat` | Sesi tanpa judul → response fallback `"New chat"` (bukan `null` mentah) |
| `test_get_chat_session_turns_empty_for_unknown_session` | Sesi tak dikenal → `turns: []`, bukan 404 |
| `test_get_chat_session_turns_returns_transcript` | Transkrip lengkap urut lama→baru |
| `test_delete_chat_session_removes_from_list_and_turns` | `DELETE` menghilangkan sesi dari daftar DAN transkripnya |

---

### `tests/test_evidence.py`

Test untuk `GET /evidence/{event_id}` (§ Evidence-Based Response, TODO.md § Prioritas 2).

| Test | Yang Diverifikasi |
|---|---|
| `test_evidence_404_for_unknown_event` | `event_id` tak dikenal → `404` |
| `test_evidence_returns_null_when_not_yet_finalized` | Event ada (`log_decision` sudah jalan) tapi `finalize` belum → `200` dengan `evidence: null`, bukan 404 |
| `test_evidence_returns_stored_payload_after_finalize` | Setelah `finalize(evidence=...)` → response mengembalikan payload persis yang tersimpan |

---

### `tests/test_approval_endpoint.py`

Test untuk `GET /approval/{approval_id}` (§ Human Approval Pipeline, TODO.md § Prioritas 2).

| Test | Yang Diverifikasi |
|---|---|
| `test_approval_404_for_unknown_id` | `approval_id` tak pernah tercatat → `404` |
| `test_approval_returns_pending_status_before_resolve` | Setelah `resolve()` → `decision` terbaca "approved" via endpoint, `tool_input` ter-decode dari JSON |
| `test_approval_status_traceable_through_full_lifecycle` | Regresi inti: `approval_id` tetap query-able setelah `decision` berubah pending→rejected (sebelumnya hilang, hanya tersirat di substring `decision` yang ditimpa) |

---

### `tests/test_role_metrics.py`

Test untuk `GET /metrics/roles` dan `POST /feedback/{event_id}` (§ Runtime Evaluation Engine, TODO.md § Prioritas 2).

| Test | Yang Diverifikasi |
|---|---|
| `test_metrics_roles_empty_initially` | Tanpa data → `{"roles": []}` |
| `test_metrics_roles_reflects_logged_events` | Event yang di-log untuk role berbeda muncul terpisah per role |
| `test_feedback_404_for_unknown_event` | `event_id` tak dikenal → `404` |
| `test_feedback_400_for_out_of_range_rating` | Rating di luar 1-5 → `400`, `ok: false` |
| `test_feedback_400_for_non_numeric_rating` | Rating bukan angka → `400` |
| `test_feedback_accepted_and_reflected_in_role_report` | Feedback sukses → `avg_human_feedback` role itu langsung terhitung di `/metrics/roles` |

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
