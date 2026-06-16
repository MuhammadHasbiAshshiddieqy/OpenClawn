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

Format SSE:
```
data: <div class='msg assistant'>    ← header pembuka
data: token1                          ← tiap token teks
data: token2
data: </div>                          ← penutup
data: [DONE]                          ← sinyal selesai
```

Karakter HTML di-escape (`&`, `<`) dan newline dikonversi ke `<br>` sebelum dikirim.

`AgentLoop` dibuat baru per request, tapi menerima singleton `approval_gate` agar HITL bisa berfungsi.

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
