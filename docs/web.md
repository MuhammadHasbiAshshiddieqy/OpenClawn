# `web/` — Web UI dan API Endpoints

FastAPI app dengan HTMX dan Server-Sent Events (SSE) streaming. Interface utama untuk berinteraksi dengan agent.

---

## `web/main.py`

### Setup

```python
db = DatabaseManager(CONFIG)
approval_gate = ApprovalGate(db, CONFIG)  # singleton level app
```

`ApprovalGate` dibuat sekali di level app — bukan per request — agar `resolve()` dari endpoint `/approve` bisa mencapai Future yang dibuat oleh request `/chat/stream` yang sedang berjalan.

### Lifespan (`lifespan`)

Dipanggil oleh FastAPI saat startup dan shutdown:
- **Startup:** setup logging, jalankan migration SQL (`migrations/001_initial.sql`)
- **Shutdown:** tutup koneksi DB

### Endpoints

---

#### `GET /`

**Halaman chat utama.**

Query params:
- `role` (default: `"pm"`) — role agent yang dipakai (single mode). Bila tidak dikenal → fallback ke role pertama.

Template: `web/templates/index.html`

Context yang dikirim ke template:
- `role` — role aktif
- `available_roles` — di-scan dinamis via `available_roles()` dari folder `roles/*/soul.toml` (urutan stabil: `_ROLE_ORDER` dulu, lalu sisanya alfabetis). Saat ini: `pm, dev, qa, data, security`.
- `roles_meta` — `ROLES_META`: map role → `[judul, deskripsi]` untuk sidebar/topbar/header bubble.
- `default_participants` — `CONFIG.conversation_default_participants` (chip peserta yang aktif secara default; role lain opt-in).
- `session_id` — UUID baru tiap halaman dimuat

---

#### `POST /chat/stream`

**Kirim pesan ke agent, terima respons via SSE streaming.**

Form data:
- `message` — pesan user (wajib, tidak boleh kosong)
- `role` — role agent (default `"pm"`)
- `session_id` — ID sesi (default UUID baru)

Response: `StreamingResponse` dengan `media_type="text/event-stream"`.

**Protokol SSE bernama** (named events) — frontend membedakan isi jawaban dari sinyal proses agar user tahu agent sedang apa, bukan diam karena macet:

```
event: status                          ← sinyal proses (tidak ditampilkan sbg jawaban)
data: {"text":"routing","detail":"gemini:gemini-2.0-flash"}

event: status
data: {"text":"thinking","detail":""}

event: thinking                        ← potongan reasoning model (blok collapsible)
data: "token nalar"

event: token                           ← potongan isi jawaban
data: token1

event: usage                           ← ringkasan turn + meter budget token (§1.4)
data: {"tokens_in":..,"tokens_out":..,"cost_usd":..,"context_tokens":..,"max_context_tokens":28000}

event: error                           ← hanya jika exception (mis. semua provider gagal)
data: {"text":"Semua provider gagal..."}

event: done                            ← selalu dikirim terakhir (finally), penanda selesai
data: [DONE]
```

Label status: `routing` (model dipilih), `thinking` (LLM mulai), `tool` (`detail`=nama tool), `fallback` (`detail`=model), `question` (`detail`=teks pertanyaan `ask_user`). Payload `status`/`error` berupa JSON; `token` dan `thinking` adalah teks MENTAH (JSON-encoded) yang dirender markdown di frontend. Event `thinking` muncul bila model mengeluarkan reasoning (`<think>` lokal, extended-thinking Anthropic, `parts.thought` Gemini) — UI menampilkannya di blok collapsible yang auto-collapse saat token jawaban pertama tiba. Event `usage` (sekali, sebelum `done`) membawa ringkasan turn + `context_tokens`/`max_context_tokens` untuk meter budget token di footer composer; meter menguning di ≥70% dan memerah di ≥90% batas. Di `/converse/stream`, budget muncul di `conversation_end.usage.peak_context_tokens` (PEAK lintas-giliran, bukan jumlah).

`event: done` selalu di-emit di blok `finally`, dan exception apa pun dari `agent.run()` ditangkap → `event: error` + di-log (`chat_stream_failed`). Jadi stream tidak pernah berakhir diam-diam tanpa penanda.

Frontend ([web/templates/index.html](../web/templates/index.html)) memisahkan dua jenis umpan balik:

- **Action chips (persisten)** — disisipkan ke dalam kolom chat, sebelum bubble jawaban, sehingga **tertinggal sebagai jejak histori**. Untuk action bermakna: `routing`, `tool`, `fallback`, dan `error`. Urutan terbaca: user → action → action → jawaban.
- **Status line (efemeral)** — di atas input box, hanya untuk sinyal sementara `thinking` ("Berpikir…") dan "Menulis jawaban…". Hilang saat turn selesai (`done`).

Plus **watchdog** 20 detik: bila tak ada frame masuk dalam jendela itu → status "⚠️ Server tidak merespons". Bila stream selesai tanpa token & tanpa error → chip persisten "⚠️ Tidak ada jawaban (semua model gagal/kosong)".

`AgentLoop` dibuat baru per request, tapi menerima singleton `approval_gate` agar HITL bisa berfungsi.

---

#### `POST /converse/stream`

**Multi-agent conversation** — beberapa role saling mengobrol, di-stream per giliran.

Form data: `message`, `pattern` (`pipeline`|`debate`|`orchestrator`), `participants` (CSV opsional), `rounds` (debate), `session_id`.

**Semantik urutan `participants`** (penting — UI mengirim chip sesuai urutan ini):
- `pipeline`: urutan = urutan handoff (mis. `dev,qa` → dev lalu qa).
- `orchestrator`: **elemen pertama = lead**, sisanya = workers. Lead tidak harus `pm` — UI menandai chip lead dengan ★ dan memindahkannya ke depan. Tanpa `participants`, default `config.conversation_default_participants` dipakai (lead = `pm`).
- `debate`: urutan giliran round-robin; `rounds` menentukan jumlah siklus.

Membangun `TurnStrategy` via `make_strategy` (`participants[0]` jadi lead untuk orchestrator), `ConversationControl(disconnect_check=request.is_disconnected)`, mendaftarkannya di `_conversations[session_id]` (registry modul-level, pola sama `ApprovalGate._pending`), lalu stream SSE. `finally`: deregister.

Frame SSE (tambahan dari `/chat/stream`):
```
event: turn               data: {"role":"pm","label":"PM","turn":0}     ← mulai giliran (UI buka bubble berlabel)
event: token              data: {"role":"pm","text":"..."}              ← token (objek, beda dari /chat/stream)
event: thinking           data: {"role":"pm","text":"..."}              ← reasoning model (blok collapsible per bubble)
event: status             data: {"role":"pm","text":"thinking","detail":""}   ← termasuk text:"question" untuk ask_user
event: conversation_end   data: {"reason":"strategy_done|max_turns|stopped","usage":{...}}
event: done               data: [DONE]
```

`usage` di `conversation_end` adalah agregat lintas-giliran: `{tokens_in, tokens_out, cost_usd, latency_ms, turns}` (UI menampilkannya sebagai ringkasan; cost ditampilkan hanya bila > 0).

#### `POST /converse/interject`

User menyela percakapan aktif. Form: `session_id`, `message` → `control.add_interjection(message)`. Disuntik ke giliran berikutnya. Return `{ok}`.

#### `POST /converse/stop`

Hentikan percakapan (cadangan; STOP utama lewat `AbortController.abort()` di frontend yang memicu `is_disconnected`). Form: `session_id` → `control.stop()`. Return `{ok}`.

---

#### `GET /approvals`

**Daftar permintaan approval yang menunggu keputusan.**

Query params:
- `session_id` (opsional) — filter per sesi

Response:
```json
{
  "pending": [
    {
      "approval_id": "abc123",
      "session_id": "...",
      "tool_name": "code_run",
      "tool_input": {"code": "..."}
    }
  ]
}
```

Dipakai Web UI untuk polling HITL — UI cek endpoint ini secara berkala dan tampilkan tombol Approve/Reject jika ada pending.

---

#### `POST /approve`

**User memutuskan approve atau reject untuk tool destruktif.**

Form data:
- `approval_id` — ID approval dari `/approvals`
- `decision` — `"approve"` atau `"reject"`

Response:
```json
{"ok": true, "approval_id": "abc123", "decision": "approve"}
```

Atau jika parameter tidak valid:
```json
{"ok": false, "error": "approval_id dan decision (approve|reject) wajib"}
```

Memanggil `approval_gate.resolve(approval_id, decision == "approve")` yang meng-unblock Future di `AgentLoop._execute_tool()`.

---

#### `POST /answer`

**User menjawab pertanyaan klarifikasi (`ask_user`).**

Form data:
- `session_id` — sesi yang sedang menunggu jawaban
- `answer` — teks jawaban user

Response: `{"ok": true}` bila ada pertanyaan pending untuk sesi itu, `{"ok": false, ...}` bila tidak.

Memanggil `question_gate.resolve_by_session(session_id, answer)` yang meng-unblock Future di `AgentLoop._execute_tool()` (jalur `ask_user`). Frontend mengirim jawaban ke sini saat status `question` aktif, alih-alih memulai chat baru. `QuestionGate` di-inject sebagai singleton dari level app (sama seperti `ApprovalGate`).

---

#### `GET /metrics`

**Dashboard kalibrasi routing.**

Template: `web/templates/metrics.html`

Menjalankan:
1. `RoutingAuditor(db).calibration_report()` — data dari DB
2. `RoutingCalibrator().summary(report)` — rekomendasi tuning + `net_offset_delta`
3. `CalibrationStore(db).get_offset()` + `.history()` — offset aktif & riwayat

Context yang dikirim:
- `report` — list data per complexity label (total, corrections, correction_rate, avg_cost)
- `calibration` — dict `{total_events, has_enough_data, net_offset_delta, recommendations, current_offset, history}`

> **Demo tanpa traffic:** dashboard ini kosong sampai ada `routing_events`. Untuk mengisinya dengan data **sintetis** (demo saja, bukan untuk tuning), jalankan `python scripts/seed_routing.py` — lihat [scripts.md](scripts.md).

---

#### `POST /calibration/apply`

**Terapkan rekomendasi kalibrasi — loop tertutup Inovasi 1.** Form: `delta` (dijepit ke `{-1,0,+1}`), `reason`.

Memanggil `CalibrationStore.apply(delta, reason, source="calibration")`: menggeser offset threshold router (disimpan di `app_settings`, dibaca `SmartRouter` tiap turn) dan mencatat baris audit ke `calibration_log`. Redirect ke `/metrics`. **Dipicu manusia** (tombol), bukan auto-apply.

#### `POST /calibration/revert`

Membatalkan kalibrasi aktif terakhir via `CalibrationStore.revert()` — mengembalikan offset ke state sebelumnya, mencatat baris `source='revert'`. Redirect ke `/metrics`.

---

#### `GET /skills`

**Visualisasi skill decay (Inovasi 2).**

Template: `web/templates/skills.html`

Membaca seluruh `skills` (semua role) langsung dari DB (read-only). Untuk setiap skill aktif, **memproyeksikan** skor decay terkini (`decay_score * base^hari_idle`) karena decay pass di-throttle 1 jam sehingga nilai tersimpan bisa stale. Skill arsip memakai skor tersimpan (final).

Context yang dikirim:
- `skills` — list dict per skill: `role`, `skill_name`, `status`, `confidence`, `use_count`, `days_idle`, `projected_score`, `score_pct`, `near_archive`
- `counts` — `{active, draft, archived}`
- `threshold`, `threshold_pct` — ambang arsip (dari `CONFIG.skill_archive_threshold`)
- `decay_base` — `CONFIG.skill_decay_base`

Bar decay tiap baris menandai garis ambang arsip; fill berubah warna (kuning→merah) saat skill mendekati arsip.

---

#### `GET /settings`

**Halaman override model.**

Template: `web/templates/settings.html`

Menampilkan dropdown pilihan model dari `KNOWN_MODELS` (gemma4 lokal, Claude, Gemini) plus opsi **Otomatis** (default). Membaca override aktif via `SettingsStore.get_model_override()`.

Query params:
- `saved` (opsional, bool) — tampilkan notifikasi "Tersimpan" setelah POST

Context yang dikirim:
- `known_models` — list `(provider, model, label)`
- `current` — `(provider, model)` jika override aktif, `None` jika mode otomatis
- `saved` — flag notifikasi

#### `POST /settings`

**Simpan pilihan model.**

Form data:
- `model_choice` — `"auto"` (kembali ke router otomatis) atau `"provider|model"` (mis. `"gemini|gemini-2.0-flash"`)

Menyimpan via `SettingsStore.set_model_override()`, lalu redirect (303) ke `/settings?saved=true`.

> **Hubungan dengan router:** Override adalah *pilihan sadar* yang memaksa semua query ke satu model — berguna untuk eksperimen (mis. memakai Gemini saja). Router otomatis (Inovasi #1) tetap default saat `model_choice=auto`. Keputusan router asli tetap tercatat di audit walaupun override aktif.

---

## `web/templates/`

### `index.html`

Template halaman chat. Fitur:
- Form input dengan HTMX (`hx-post="/chat/stream"`, `hx-target="#chat-box"`, `hx-swap="beforeend"`)
- SSE streaming dengan HTMX SSE extension
- Role selector (pm/qa/dev)
- Session ID di-pass sebagai hidden input

### `metrics.html`

Template dashboard `/metrics`. Menampilkan:
- Tabel data per complexity label: total event, koreksi, correction rate, avg cost
- Bagian **Tuning Recommendations** (dari `RoutingCalibrator`):
  - Badge untuk "cukup data" vs "belum cukup data"
  - Daftar rekomendasi: label, issue (under/over-provisioned), sample size, saran teks
  - Offset threshold aktif + tombol **Apply**/**Revert** (loop tertutup) + tabel riwayat kalibrasi

### `skills.html`

Template dashboard `/skills`. Menampilkan:
- Count chip active/draft/archived
- Tabel per skill: nama, role (role-dot), status, bar decay terproyeksi dengan garis ambang arsip, hari idle, use_count, confidence

### `settings.html`

Template halaman `/settings`. Menampilkan:
- Dropdown pilihan model (opsi **Otomatis** + daftar `KNOWN_MODELS`)
- Status mode aktif (otomatis vs override `provider/model`)
- Catatan kebutuhan API key (`.env`) untuk model cloud dan Ollama untuk model lokal

---

## Static Files

`/static/` → direktori `web/static/`  
Mount ke FastAPI untuk CSS, JS, gambar, dll.

---

## Cara Menjalankan

```bash
# Development
uvicorn web.main:app --reload --port 8000

# Production (tanpa reload)
uvicorn web.main:app --port 8000 --workers 1
```

> Single worker karena `AgentLoop` menyimpan history di memory (per-instance). Multi-worker membutuhkan session store eksternal.

Buka:
- `http://localhost:8000` — chat interface
- `http://localhost:8000/metrics` — routing calibration dashboard (+ apply/revert kalibrasi)
- `http://localhost:8000/skills` — skill decay dashboard
- `http://localhost:8000/settings` — override model
