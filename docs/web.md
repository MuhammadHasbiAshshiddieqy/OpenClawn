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
- `role` (default: `"pm"`) — role agent yang dipakai

Template: `web/templates/index.html`

Context yang dikirim ke template:
- `role` — role aktif
- `available_roles` — `["pm", "qa", "dev"]`
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

event: token                           ← potongan isi jawaban
data: token1

event: error                           ← hanya jika exception (mis. semua provider gagal)
data: {"text":"Semua provider gagal..."}

event: done                            ← selalu dikirim terakhir (finally), penanda selesai
data: [DONE]
```

Label status: `routing` (model dipilih), `thinking` (LLM mulai), `tool` (`detail`=nama tool), `fallback` (`detail`=model). Payload `status`/`error` berupa JSON; `token` adalah teks yang sudah di-escape (`&`, `<`) dengan newline → `<br>`.

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

Membangun `TurnStrategy` via `make_strategy`, `ConversationControl(disconnect_check=request.is_disconnected)`, mendaftarkannya di `_conversations[session_id]` (registry modul-level, pola sama `ApprovalGate._pending`), lalu stream SSE. `finally`: deregister.

Frame SSE (tambahan dari `/chat/stream`):
```
event: turn               data: {"role":"pm","label":"PM","turn":0}     ← mulai giliran (UI buka bubble berlabel)
event: token              data: {"role":"pm","text":"..."}              ← token (objek, beda dari /chat/stream)
event: status             data: {"role":"pm","text":"thinking","detail":""}
event: conversation_end   data: {"reason":"strategy_done|max_turns|stopped"}
event: done               data: [DONE]
```

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

#### `GET /metrics`

**Dashboard kalibrasi routing.**

Template: `web/templates/metrics.html`

Menjalankan:
1. `RoutingAuditor(db).calibration_report()` — data dari DB
2. `RoutingCalibrator().summary(report)` — rekomendasi tuning

Context yang dikirim:
- `report` — list data per complexity label (total, corrections, correction_rate, avg_cost)
- `calibration` — dict `{total_events, has_enough_data, recommendations}`

> **Demo tanpa traffic:** dashboard ini kosong sampai ada `routing_events`. Untuk mengisinya dengan data **sintetis** (demo saja, bukan untuk tuning), jalankan `python scripts/seed_routing.py` — lihat [scripts.md](scripts.md).

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
- `http://localhost:8000/metrics` — routing calibration dashboard
