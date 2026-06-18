# `core/` — Otak Agent

Modul core berisi logika utama: agent loop, LLM client, router, audit, crystallizer, compactor, calibration advisor, dan multi-agent conversation.

---

## `core/conversation.py` — Multi-Agent Conversation

Beberapa agent (role) saling mengobrol. Ide inti: **percakapan = urutan giliran agent; sebuah `TurnStrategy` memutuskan siapa bicara berikutnya & kapan berhenti.** Tiga pola = tiga strategy di atas satu orchestrator. Modul extractable: hanya bergantung `DatabaseManager`, `AppConfig`, `agent_factory` — tanpa import web. Tiap giliran = `AgentLoop.run()` penuh.

### Dataclass: `ConversationEvent`

| Field | Keterangan |
|---|---|
| `type` | `"turn"` (mulai giliran → UI buka bubble baru) / `"token"` / `"status"` / `"conversation_end"` |
| `role` | Role yang sedang bicara |
| `text`, `detail` | Seperti `AgentEvent`; untuk `turn` text=label role, untuk `conversation_end` detail=alasan |
| `turn_index` | Ordinal giliran (0-based) |

### Dataclass: `ConversationState`
Transkrip `(role, content)` lintas giliran, `last_output` (contract tervalidasi terakhir atau `{"text": raw}`), `turn_index`, `round_index`.

### Kelas: `ConversationControl`
Kontrol STOP + INTERJECT, web-agnostic. `stop()`, `add_interjection(text)`, `pop_interjection()`, `is_stopped() → bool` *(async)* (cek flag + `disconnect_check` opsional yang di-wire ke `request.is_disconnected`).

### Kelas: `TurnStrategy` (ABC) + turunannya
- `next_speaker(state) → str | None` — role berikutnya, `None` = selesai.
- `build_turn_input(state, role, interjection) → str` — rakit prompt giliran.
- `wants_contract(role) → bool` — apakah output divalidasi vs `CONTRACT_REGISTRY`.

| Strategy | Pola | Terminasi |
|---|---|---|
| `PipelineStrategy(participants)` | Urut sekali: pm→dev→qa. Suapkan output role sebelumnya; `wants_contract=True`. | Setelah participant terakhir |
| `DebateStrategy(participants, rounds)` | Round-robin `rounds` siklus; suapkan transkrip penuh (free text). | Setelah `rounds × len(participants)` giliran |
| `OrchestratorStrategy(lead, workers)` | Lead delegasi dinamis via directive JSON (`{"delegate_to","task"}`/`{"done":true}`). Setelah worker → balik ke lead. | Lead `done`, atau **fallback** alur tetap (lead→workers→lead) bila directive tak terbaca |

### Kelas: `ConversationOrchestrator`
`__init__(strategy, db, agent_factory, session_id, config=CONFIG, control=None)`.

**`run(initial_message) → AsyncGenerator[ConversationEvent, None]`** *(async)*
Loop sampai `max_conversation_turns`/strategy selesai/STOP. Per giliran: cek stop → `next_speaker` → rakit input (+interjection) → emit `turn` → jalankan `agent_factory(role).run()` (re-wrap token/status, cek stop tiap event) → bila `wants_contract` validasi + tulis `role_handoffs` (**gagal → tetap lanjut dgn teks mentah**, keputusan degrade-graceful).

### Fungsi: `make_strategy(pattern, participants, rounds, config) → TurnStrategy`
Bangun strategy dari parameter request (`pipeline`/`debate`/`orchestrator`); default participants dari config.

---

## `core/agent_loop.py`

Entry point utama untuk setiap percakapan. Setiap request dari Web UI menginstansiasi `AgentLoop` baru.

### Dataclass: `AgentConfig`

Konfigurasi per-sesi agent.

| Field | Keterangan |
|---|---|
| `role` | Role aktif: `"pm"`, `"qa"`, atau `"dev"` |
| `session_id` | ID sesi unik (UUID) |
| `user_id` | ID user (default `"default"` — single-user mode) |

### Dataclass: `Turn`

Merepresentasikan satu turn percakapan.

| Field | Keterangan |
|---|---|
| `role` | `"user"` atau `"assistant"` |
| `content` | Teks konten turn |
| `tool_calls` | Daftar tool yang dipanggil dalam turn ini |
| `tokens_in` | Token input dikonsumsi |
| `tokens_out` | Token output dihasilkan |
| `model_used` | Nama model yang dipakai |
| `cost_usd` | Estimasi biaya dalam USD |
| `latency_ms` | Latensi total turn dalam milidetik |
| `fallback_used` | Apakah fallback chain aktif |

### Dataclass: `AgentEvent`

Event yang di-stream `run()` ke Web UI. Memisahkan isi jawaban dari sinyal proses agar user tahu agent sedang apa (tidak terlihat menggantung).

| Field | Keterangan |
|---|---|
| `type` | `"token"` (jawaban), `"thinking"` (reasoning model), `"status"` (sinyal proses), atau `"usage"` (ringkasan biaya turn di akhir) |
| `text` | Untuk `token`/`thinking`: isi teks. Untuk `status`: label (`routing`/`thinking`/`tool`/`fallback`/`question`/`loop_stopped`) |
| `detail` | Konteks status opsional (mis. `provider:model` saat `routing`, nama tool saat `tool`, teks pertanyaan saat `question`) |
| `usage` | Untuk `usage`: `{tokens_in, tokens_out, cost_usd, latency_ms, model}` |

> `type="thinking"` di-emit dari reasoning model dan ditampilkan di blok collapsible terpisah di UI — **tidak** masuk `turn.content` (bukan jawaban final, jadi tidak di-crystallize/diarsipkan).

### Kelas: `AgentLoop`

Orkestrasi lengkap satu sesi percakapan.

**`__init__(agent_cfg, db, config=CONFIG, approval=None)`**  
Inisialisasi semua komponen: LLM client, memory manager, skill decay, router, auditor, compactor, crystallizer, approval gate, dan shield. Muat `soul.toml` sekali saat init (di-cache di `self._soul`).

> `approval` harus di-inject dari level app (singleton `ApprovalGate`) agar endpoint `/approve` bisa me-resolve Future yang sama yang ditunggu di sini.

**`run(user_message: str) → AsyncGenerator[AgentEvent, None]`** *(async generator)*  
Pipeline utama per turn. Menghasilkan `AgentEvent` (`token` + `status`) ke Web UI via SSE. Status di-emit di titik kunci: `routing` (setelah model dipilih), `thinking` (sebelum tiap stream LLM), `tool` (sebelum eksekusi tool), `fallback` (saat fallback chain aktif). Urutan:

1. **Shield scan** — tolak input mencurigakan sebelum masuk pipeline
2. **Correction check** — deteksi apakah turn sebelumnya dikoreksi user (audit feedback)
3. **Load active skills** — ambil skill dari decay manager (Inovasi 2)
4. **Load memory context** — L1/L2/L3/L4 (Inovasi 2 lanjutan)
5. **Build messages** — compactor merakit context dengan budget token
6. **Route + log decision** — soul-aware routing, catat ke audit DB (Inovasi 1). Jika ada **model override** dari `/settings` (`SettingsStore.get_model_override()`), provider/model dipaksa ke pilihan itu — keputusan router asli tetap tercatat di `reason` untuk transparansi audit
7. **Tool loop** — iterasi tool call (tidak rekursif)
8. **Finalize** — update audit record dengan latensi, cost, token
9. **Post-turn** — background task: tulis L1 checkpoint, arsip L4, decay pass, crystallize

**`_run_tool_loop(messages, route, tools_schema, turn) → AsyncGenerator[AgentEvent, None]`** *(async generator, private)*  
Loop iteratif (bukan rekursif) untuk menangani tool call. Meng-emit `AgentEvent` (`thinking`/`token`/`tool`/`fallback`). Berhenti saat tidak ada tool call pending atau `max_tool_hops` tercapai.

**`_execute_tool(name, input_data) → dict`** *(async, private)*  
Eksekusi satu tool dengan jaring pengaman §1.3: cek keberadaan → cek izin role → **validasi input vs schema** (`_validate_tool_input`) → approval jika perlu → jalankan dalam `asyncio.wait_for(timeout=tool_timeout_sec)` dengan try/except yang mengubah exception/timeout apa pun menjadi `{"error": ...}` anggun → potong output seragam (`_truncate_tool_output`) → catat telemetri (`ToolAudit.record`). Satu tool yang gagal/menggantung tidak menjatuhkan turn.

**`_truncate_tool_output(result) → dict`** *(private)*  
Potong field teks hasil tool yang melebihi `tool_max_output` (token-first §1.4) — jaring akhir agar tidak ada tool yang membanjiri context.

**`_validate_tool_input(tool, input_data) → str | None`** *(module-level)*  
Validasi ringan input vs `input_schema` (required fields ada & non-kosong). Return pesan error (dikirim balik ke model agar memperbaiki) atau `None`. Bukan validator JSON-Schema penuh — cukup menangkap kesalahan umum model lokal tanpa dependency.

**`_tool_allowed(name) → bool`** *(private)*  
Cek apakah tool ada di daftar `soul.toml[tools][allowed]` untuk role aktif.

**`_tools_for_role() → list`** *(private)*  
Kembalikan schema hanya untuk tool yang diizinkan role ini (hemat token).

**`_post_turn(user_message, turn, active_skills, history_snapshot) → None`** *(async, private)*  
Background task yang berjalan setelah turn selesai. Tidak memblokir SSE stream:
- Tulis L1 checkpoint jika turn punya konten (tiap turn)
- Arsip ke L4 jika history sudah cukup panjang (`archive_after_turns`)
- Jalankan decay pass (throttled)
- Crystallize jika syarat terpenuhi (≥ 3 tool calls)

**`_post_turn_done(task) → None`** *(private)*  
Callback `add_done_callback` untuk menangkap error dari background task `_post_turn`.

**`_render_history(history) → str`** *(static, private)*  
Serialisasi list `Turn` menjadi teks untuk arsip L4.

**`_load_soul_once() → dict`** *(private)*  
Baca `roles/{role}/soul.toml` dan cache hasilnya. Dipanggil sekali di `__init__`.

---

## `core/llm_client.py`

**Entry point tunggal untuk semua interaksi LLM.** Tidak ada modul lain yang boleh memanggil Ollama/Claude langsung.

### Dataclass: `LLMChunk`

Unit terkecil output streaming dari LLM.

| Field | Nilai `type` | Keterangan |
|---|---|---|
| `type` | `"text"` | Token jawaban final |
| `type` | `"thinking"` | Token reasoning model (lihat ThinkTagSplitter & parser) |
| `type` | `"tool_call"` | LLM ingin panggil tool |
| `type` | `"usage"` | Data penggunaan token |
| `type` | `"fallback"` | Sinyal bahwa fallback aktif |
| `text` | — | Konten teks (untuk type `text`/`thinking`) |
| `tool_name` | — | Nama tool (untuk type `tool_call`) |
| `tool_input` | — | Input tool sebagai dict |
| `usage` | — | Dict `{input_tokens, output_tokens}` |
| `fallback_used` | — | True jika fallback aktif |
| `fallback_model` | — | Nama model fallback yang dipakai |

### Kelas: `ThinkTagSplitter`

Memisahkan reasoning inline `<think>...</think>` dari teks jawaban secara **streaming-safe**. Model GGUF lokal (deepseek-r1, qwen, gemma) menaruh nalar di dalam `<think>` di tengah content; karena di-stream token demi token, tag bisa terpotong (`<thi` lalu `nk>`). Splitter menahan ekor yang berpotensi bagian tag sampai pasti.

**`feed(chunk) → list[tuple[str, str]]`** — proses satu potongan stream → daftar `(kind, text)` dengan `kind ∈ {"thinking","text"}`.
**`flush() → list[tuple[str, str]]`** — emit sisa buffer di akhir stream (tag tak tertutup → diperlakukan apa adanya).

### Exception: `ProviderUnavailable`

Di-raise ketika semua provider dalam fallback chain gagal.

### Kelas: `LLMClient`

**`__init__(vault, config)`**  
Terima `Vault` untuk mengambil API key saat dibutuhkan.

**`stream_with_fallback(provider, model, messages, tools=None, max_tokens=4096) → AsyncGenerator[LLMChunk, None]`** *(async generator)*  
Satu-satunya method publik. Coba `(provider, model)` utama, jika gagal turun ke `config.fallback_chain`. Setiap fallback yang aktif menghasilkan `LLMChunk(type="fallback")` sebagai sinyal ke consumer (untuk audit logging).

- Retry hanya untuk `httpx.HTTPError` (transient). Error logika tidak di-retry.
- Untuk Anthropic: system prompt di-wrap dengan `cache_control: ephemeral` untuk prompt caching (hemat hingga 90% biaya bagian statis).

**`_health_check(provider) → bool`** *(async, private)*  
Ping health endpoint sebelum call. Ollama: `GET /api/tags`. Anthropic: asumsikan up (retry handle transient).

**`_stream_one(provider, model, messages, tools, max_tokens) → AsyncGenerator[LLMChunk, None]`** *(async generator, private, dengan `@retry`)*  
Retry dengan exponential backoff (tenacity). Dispatch ke `_ollama()` atau `_claude()`.

**`_ollama(model, messages, tools, max_tokens) → AsyncGenerator[LLMChunk, None]`** *(async generator, private)*  
Streaming request ke `POST /api/chat` Ollama. Parse NDJSON response.

> **Plaintext tool call parsing:** Banyak model GGUF lokal (Gemma, Qwen, Llama, Mistral, DeepSeek)
> mengeluarkan tool call sebagai token teks di stream `content`, bukan sebagai field JSON
> `message.tool_calls`. `_ollama` kini me-*buffer* seluruh teks stream, lalu memanggil
> `parse_plaintext_tool_calls()` di akhir untuk mendeteksi 7 format tool call berbeda.
> Tool call yang terdeteksi di-prioritaskan bersama native `tool_calls`. Hasil teks
> dibersihkan dari token tool call sebelum dikirim ke user.

**`parse_plaintext_tool_calls(text) → tuple[str, list[dict]]`** *(static method)*  
Parse 7 format plaintext tool call dari berbagai keluarga model:

| Format | Keluarga Model | Pola |
|---|---|---|
| `gemma` | Gemma 4 | `<\|tool_call>call:NAME{args}<tool_call\|>` |
| `qwen` | Qwen 2.5/3 | `<tool_call>{"name":...,"arguments":...}</tool_call>` |
| `llama3` | Llama 3.1/3.2 | `<\|python_tag\|>{"name":...,"parameters":...}` |
| `mistral` | Mistral/Mixtral | `[TOOL_CALLS] [{"name":...,"arguments":...}]` |
| `deepseek` | DeepSeek | `<｜tool▁call▁begin｜>{...}<｜tool▁call▁end｜>` |
| `functionary` | Functionary v3 | `<\|from\|>assistant\n<\|recipient\|>NAME\n<\|content\|>{args}\n<\|stop\|>` |
| `tool_code` | Generic | `<tool_code>NAME{args}</tool_code>` |

Return: `(cleaned_text, [{"name": "...", "input": {...}}, ...])` — teks bersih tanpa token tool call + daftar parsed call.

> **Reasoning/thinking:** `_ollama` melewatkan content lewat `ThinkTagSplitter` (memisahkan `<think>…</think>` → `LLMChunk(type="thinking")`) dan juga menangkap field `message.thinking` terpisah (API Ollama baru). Hanya bagian non-thinking yang masuk buffer deteksi tool call.

**`_claude(model, messages, tools, max_tokens) → AsyncGenerator[LLMChunk, None]`** *(async generator, private)*  
Streaming request ke `POST /v1/messages` Anthropic. Parse SSE response (`data:` lines). `text_delta` → `text`; `thinking_delta` (extended thinking) → `LLMChunk(type="thinking")`. API key diambil dari `Vault` tepat sebelum request — tidak pernah di-cache di memori lebih lama dari perlu.

**`_gemini(model, messages, max_tokens) → AsyncGenerator[LLMChunk, None]`** *(async generator, private)*  
Streaming request ke Google AI Studio (`POST /v1beta/models/{model}:streamGenerateContent?alt=sse`). API key (`GOOGLE_API_KEY`) diambil dari `Vault`, dikirim via header `x-goog-api-key`. Mengonversi format internal (`system`/`assistant`) ke format Gemini (`systemInstruction` + `contents` dengan peran `user`/`model`). Parse SSE → `candidates[].content.parts[].text` dan `usageMetadata`; `parts[]` dengan `thought=true` → `LLMChunk(type="thinking")`. Tool calling belum didukung di jalur Gemini (cukup teks — audit/crystallizer yang butuh JSON teks tetap jalan).

**Provider yang didukung:** `ollama`, `anthropic`, `gemini`.

> **Tidak ada SDK.** Raw `httpx` dipakai secara sengaja untuk transparansi audit — setiap header dan payload bisa diperiksa. Gemini pun lewat raw httpx, bukan SDK Google.

---

## `core/router.py`

Router yang membaca kepribadian role (`soul.toml`) untuk memilih model yang tepat.

### Enum: `Complexity`

Lima level kompleksitas query:
`TRIVIAL` → `SIMPLE` → `MODERATE` → `COMPLEX` → `CRITICAL`

### Dataclass: `RouteDecision`

Keputusan routing yang dikembalikan `SmartRouter.decide()`.

| Field | Keterangan |
|---|---|
| `model` | Nama model yang dipilih |
| `provider` | `"ollama"`, `"anthropic"`, atau `"gemini"` |
| `complexity` | Level `Complexity` yang dipilih |
| `complexity_score` | Skor numerik sebelum di-label |
| `reason` | Penjelasan teks keputusan routing |
| `cost_per_1k` | Estimasi biaya per 1000 token (USD) |
| `dimensions` | Dict 8 dimensi input (untuk audit) |
| `soul_upgrade_hit` | True jika soul upgrade_keyword cocok |

### Kelas: `SmartRouter`

**`__init__(role, soul_path=None, threshold_offset=0)`**  
Baca `soul.toml` sekali dan ekstrak `prefer_local` serta `upgrade_keywords`. `threshold_offset` = offset kalibrasi global (loop tertutup #1): negatif → router naik tier lebih cepat, positif → bertahan tier murah lebih lama, `0` → perilaku asli. `AgentLoop` menyetel `router.threshold_offset = await CalibrationStore.get_offset()` sebelum tiap `decide()`.

**`decide(messages, query) → RouteDecision`**  
Hitung dimensi → skor → label → pilih model. Soul upgrade_keyword menambah +3 ke skor dan **bypass** `prefer_local`. `threshold_offset` kalibrasi selalu berlaku (termasuk saat soul hit).

**`_dimensions(messages, query) → dict`** *(private)*  
Hitung 8 dimensi dari query dan history:
- `query_tokens` — estimasi token query
- `has_tech_kw` — mengandung kata teknis?
- `needs_multistep` — butuh analisis multi-langkah?
- `history_len` — panjang history
- `role` — role aktif
- `has_urgency` — ada kata urgensi?
- `needs_stream` — selalu 1
- `is_continuation` — apakah lanjutan (history > 2)?

**`_score(dimensions) → int`** *(private)*  
Konversi dimensi ke skor numerik.

**`_label(score, threshold_shift) → Complexity`** *(private)*  
Map skor ke label. `threshold_shift` menggabungkan `prefer_local` (+1, lebih lama di Ollama) dan `threshold_offset` kalibrasi.

**`_explain(complexity, soul_hit) → str`** *(private)*  
Teks penjelasan untuk audit record.

**Peta model** (setup utama LOKAL — tier ringan→sedang dilayani Ollama, tier berat naik ke Gemini cloud):

| Complexity | Model | Provider |
|---|---|---|
| TRIVIAL | `gemma4:e4b` | Ollama |
| SIMPLE | `deepseek-r1:latest` | Ollama |
| MODERATE | `qwen3.5:9b` | Ollama |
| COMPLEX | `gemini-2.5-flash` | Gemini |
| CRITICAL | `gemini-2.5-pro` | Gemini |

> Tier lokal dibedakan **per kapasitas model** — makin sulit case, makin mampu model (gemma4:e4b ringan → deepseek-r1 → qwen3.5:9b paling mampu lokal). Fallback chain mengikuti urutan yang sama. Remap lewat `MODELS` dict + `config.fallback_chain`.

---

## `core/audit.py` — Inovasi 1

Mencatat setiap keputusan routing dan apakah terbukti tepat.

### Konstanta: `CORRECTION_SIGNALS`

Daftar kata/frasa yang menandakan user mengoreksi respons sebelumnya (sinyal feedback). Mencakup Indonesia & English (core locale-neutral §1.5).

### Kelas: `RoutingAuditor`

**`log_decision(session_id, role, query, route) → int`** *(async)*  
Catat keputusan routing ke tabel `routing_events` **sebelum** LLM call. Return `lastrowid` (dipakai sebagai `event_id` untuk `finalize`). Semua 8 dimensi dicatat.

**`finalize(event_id, turn) → None`** *(async)*  
Update record dengan hasil aktual **setelah** turn selesai: token in/out, cost, latensi, fallback flag.

**`check_correction(user_message, session_id) → None`** *(async)*  
Dipanggil di **awal setiap turn** (oleh `AgentLoop.run`). Jika pesan user mengandung sinyal koreksi, update record turn **sebelumnya** di session yang sama dengan `had_correction=1`. Aman dipanggil selalu — UPDATE hanya kena bila ada event sebelumnya untuk session. (Tidak boleh di-gate `self.history`: AgentLoop dibuat baru tiap request web → history selalu kosong → koreksi tak pernah terdeteksi.)

**`calibration_report() → list[dict]`** *(async)*  
Agregasi per `complexity_label`: total event, jumlah koreksi, correction rate (%), avg cost. Dipakai oleh `/metrics` dan `RoutingCalibrator`.

---

## `core/crystallizer.py` — Inovasi 3

Agent menilai kualitas solusinya sendiri sebelum menyimpan sebagai skill.

### Konstanta

- `MIN_TOOL_CALLS = 3` — syarat minimum tool call sebelum crystallize dicoba
- `CONFIDENCE_THRESHOLD = 4` — batas bawah confidence (dari 5)
- `EVALUATOR_FOR` — map dari generator model ke evaluator model

**Aturan kritis:** evaluator harus **minimal setara** generator. Solusi Sonnet tidak boleh dinilai model 7B.

```python
EVALUATOR_FOR = {
    "gemma4:e2b":  ("ollama",     "gemma4:e4b"),
    "gemma4:e4b":  ("ollama",     "gemma4:12b"),
    "gemma4:12b":  ("anthropic",  "claude-haiku-4-5-20251001"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-sonnet-4-6":         ("anthropic", "claude-sonnet-4-6"),
}
```

### Kelas: `ConfidenceCrystallizer`

**`should_attempt(history) → bool`**  
Return True jika total tool call dalam history ≥ `MIN_TOOL_CALLS`.

**`crystallize(task, solution, history, generator_model) → dict`** *(async)*  
Proses crystallization lengkap:
1. Pilih evaluator dari `EVALUATOR_FOR`
2. Jalankan self-evaluation via LLM
3. Tentukan status: `"active"` jika confidence ≥ 4 dan tidak ada critical gaps, `"draft"` jika tidak
4. Simpan ke tabel `skills`

Return dict dengan `skill_name`, `status`, `evaluator`, `confidence`, `critical_gaps`, `reasoning`.

**`_self_evaluate(task, solution, provider, model) → dict`** *(async, private)*  
Prompt evaluator model untuk menilai solusi dalam format JSON ketat: `{confidence, critical_gaps, reasoning}`.

**`_parse(raw) → dict`** *(private)*  
Parse JSON response evaluator. Jika parse gagal → fail-safe ke `confidence=1, critical_gaps=True` (skill tidak masuk active).

**`_format(task, steps, solution, ev) → str`** *(private)*  
Render konten skill sebagai markdown terstruktur.

**`_slug(task) → str`** *(private)*  
Buat nama skill dari 5 kata pertama task (`"task-langkah-pertama"`).

---

## `core/compactor.py`

Merakit `messages` list dengan batas token. Memastikan context tidak melebihi `max_context_tokens`.

### Fungsi: `_estimate_tokens(text) → int`

Heuristik `len(text) // 4` (±4 karakter per token). Tidak butuh dependency tiktoken.

### Kelas: `ContextCompactor`

**`build(soul, memory, history, user_message) → list[dict]`**  
Rakit messages dengan urutan:
1. System prompt (soul + memory block)
2. History turns (dari terbaru, potong jika budget habis, maks 20 turns)
3. User message baru

**`estimate_context_tokens(messages) → int`**  
Estimasi token total context window (prompt-side) dengan heuristik yang sama dengan trimming. Dipakai `AgentLoop` untuk memancarkan meter budget token (§1.4) di event `usage` (`context_tokens` + `max_context_tokens`), dirender frontend sebagai bar yang menguning/memerah saat mendekati batas.

**`_build_system(soul, memory) → str`** *(private)*  
Gabungkan soul prompt dengan memory yang relevan:
- `## State` dari L1 (max 20 item)
- `## Facts` dari L2 (max 10 fakta, urut importance)
- `## Active Skills` dari L3 (max 5 skill)
- `## Past Sessions` dari L4 (max 3 arsip)

---

## `core/calibration.py` — Inovasi 1 (lanjutan)

Dua bagian: `RoutingCalibrator` (advisor MURNI, tanpa DB) menerjemahkan data audit menjadi rekomendasi; `CalibrationStore` (DB-bound) menutup loop dengan menyimpan offset threshold aktif + jejak audit. **Apply tetap keputusan manusia** (tombol di `/metrics`) — bukan auto-apply.

### Konstanta

| Konstanta | Nilai | Keterangan |
|---|---|---|
| `MIN_SAMPLE_FOR_SIGNAL` | `10` | Sampel minimum agar saran dianggap valid |
| `HIGH_CORRECTION_RATE` | `20.0` | % koreksi → under-provisioned |
| `LOW_CORRECTION_RATE` | `5.0` | % koreksi → over-provisioned (khusus label cloud) |
| `CLOUD_LABELS` | `{"complex", "critical"}` | Label yang pakai Claude (berbiaya) |
| `ROUTER_OFFSET_KEY` | `"router_threshold_offset"` | Key di `app_settings` tempat offset aktif disimpan (dibaca `SmartRouter`) |
| `OFFSET_MIN`, `OFFSET_MAX` | `-3`, `3` | Batas offset agar kalibrasi tak pernah ekstrem |

### Dataclass: `Recommendation`

| Field | Keterangan |
|---|---|
| `label` | Complexity label yang disarankan untuk diubah |
| `issue` | `"under_provisioned"` atau `"over_provisioned"` |
| `correction_rate` | Correction rate (%) label ini |
| `sample_size` | Jumlah sampel yang dianalisis |
| `suggestion` | Teks saran yang bisa dibaca manusia |
| `offset_delta` | Arah geser offset disarankan: `-1` (under, naik tier lebih cepat) atau `+1` (over, bertahan murah) |

### Kelas: `RoutingCalibrator`

Murni dan extractable: input berupa `list[dict]` dari `calibration_report()`, tidak sentuh DB langsung.

**`__init__(min_sample, high_rate, low_rate)`**  
Parameter bisa di-override untuk kebutuhan testing.

**`analyze(report) → list[Recommendation]`**  
Loop seluruh report:
- Skip jika sampel < `min_sample`
- `rate >= high_rate` → `under_provisioned` + saran naik level
- `rate <= low_rate` DAN label cloud DAN ada cost → `over_provisioned` + saran turun level

**`summary(report) → dict`**  
Return dict siap-tampil untuk endpoint `/metrics`:
```json
{
  "total_events": 42,
  "has_enough_data": true,
  "net_offset_delta": -1,
  "recommendations": [...]
}
```
`net_offset_delta` = arah geser global (jumlah `offset_delta` semua saran, dijepit ke `{-1,0,+1}`). Frontend memakainya untuk tombol Apply satu-klik; `0` artinya saran saling meniadakan.

**`_suggest_upgrade(label) → str`** *(private)*  
Teks saran untuk label under-provisioned: arahkan ke tier di atasnya.

**`_suggest_downgrade(label) → str`** *(private)*  
Teks saran untuk label cloud over-provisioned: turun ke tier lebih murah.

**`_neighbor(label, direction) → str | None`** *(private)*  
Kembalikan label sebelum/sesudah dalam `COMPLEXITY_ORDER`, atau `None` jika sudah di ujung.

### Kelas: `CalibrationStore`

DB-bound (hanya bergantung `DatabaseManager`, §1.6). Mengelola offset threshold aktif + jejak audit untuk loop tertutup. Offset aktif disimpan di `app_settings[ROUTER_OFFSET_KEY]` dan dibaca `SmartRouter` setiap turn (`AgentLoop` set `router.threshold_offset` sebelum `decide()`).

**`get_offset() → int`** *(async)*  
Offset aktif (default `0` = router asli). Fail-safe ke `0` bila nilai korup — tidak pernah meng-crash router.

**`apply(delta, reason, source="calibration") → dict`** *(async)*  
Geser offset sebesar `delta` (dijepit ke `[OFFSET_MIN, OFFSET_MAX]`), nonaktifkan baris audit aktif sebelumnya, tulis baris baru `active=1` ke `calibration_log`, update `app_settings`. Return `{old_offset, new_offset, changed}`.

**`revert() → dict`** *(async)*  
Kembalikan offset ke `old_offset` dari baris aktif terakhir; catat baris `source='revert'`. No-op bila tak ada riwayat. Audit lama tetap utuh (tidak dihapus).

**`history(limit=20) → list[dict]`** *(async)*  
Riwayat perubahan offset terbaru-dulu untuk ditampilkan di `/metrics`.

---

## `core/tool_audit.py` — Telemetri Tooling

Audit penggunaan tool (setara Inovasi 1 untuk routing). DB-bound (hanya `DatabaseManager`, §1.6), extractable. Dicatat terpusat di `AgentLoop._execute_tool`, bukan per-tool.

### Kelas: `ToolAudit`

**`record(session_id, role, tool_name, outcome, latency_ms)`** *(async)*  
Catat satu eksekusi ke tabel `tool_invocations`. **Fail-soft**: kegagalan menulis hanya di-log, tidak diteruskan — telemetri tak boleh menjatuhkan turn.

**`summary() → list[dict]`** *(async)*  
Agregasi per tool untuk `/metrics`: `total`, `errors`, `timeouts`, `fail_rate` (%), `avg_latency_ms`. Diurut paling sering dipakai dulu.
