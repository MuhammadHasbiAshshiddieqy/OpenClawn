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
- **Startup:** setup logging, jalankan migration SQL (`migrations/001_initial.sql`), **startup health check** (§P0 production-readiness) — cek Ollama reachability + API key cloud yang terkonfigurasi, di-LOG (`startup_health`) TIDAK memblokir boot (§8: "Ollama offline ≠ agent mati"); warning terpisah bila tak ada provider LLM sama sekali terjangkau, atau bila `OPENCLAWN_AUTH_TOKEN` kosong (auth nonaktif)
- **Shutdown:** tutup koneksi DB

### Middleware (`auth_and_csrf_middleware`)

Self-host auth + CSRF + rate limit (§P0 production-readiness, `security/auth.py` +
`security/rate_limit.py`). **Fail-open**: `CONFIG.auth_token` kosong (default) → seluruh
middleware di-skip, perilaku lama tanpa login tetap jalan. Aktif hanya bila
`OPENCLAWN_AUTH_TOKEN` diisi:
1. Sesi tak valid → redirect `/login` (GET) atau 401 JSON (non-GET). `/health`, `/login`,
   `/static/*` selalu publik.
2. POST form biasa tanpa `csrf_token` cocok (cookie vs field form) → 403 JSON. Endpoint
   SSE/fetch JS (`/chat/stream`, `/converse/stream`, `/converse/interject`,
   `/converse/stop`, `/answer`, `/approve`) di-exempt — dilindungi cookie auth +
   `SameSite=lax`, bukan form CSRF biasa.
3. `/chat/stream` & `/converse/stream` dibatasi `RateLimiter` (default 20/60 detik per
   sesi) → 429 JSON + header `Retry-After` bila terlampaui.

### Exception handlers

**404** (`StarletteHTTPException`, status 404) → render `404.html` (ramah, tanpa detail
internal). **Unhandled exception** (`Exception`) → log traceback server-side
(`unhandled_exception`), render `500.html` — **tidak pernah** membocorkan stack trace ke
client.

### Endpoints

---

#### `GET /health`

**Health check** untuk monitoring self-hosted (single-user, §7) + Docker healthcheck.
Verifikasi DB (`SELECT 1` murah) + Ollama (`GET /api/tags`, timeout 2s) + key cloud yang
terkonfigurasi. Return JSON `{ok, service, database: "up"|"down", ollama: "up"|"down",
cloud_keys: {anthropic, gemini}, auth_enabled, tools}`. `ok` hanya bergantung DB — Ollama
down TIDAK membuat `ok=False` (fallback chain menangani), dilaporkan terpisah di `ollama`.
Dipakai oleh `docker-compose.yml` healthcheck (stdlib `urllib`, bukan `curl` — image slim
tak menyertakannya).

---

#### `GET /login`, `POST /login`, `POST /logout`

**Self-host auth** (§P0 production-readiness, opt-in via `OPENCLAWN_AUTH_TOKEN`).

`GET /login` → redirect `/` bila auth nonaktif; render `login.html` (form password,
query `?next=` dibawa sebagai hidden field, `?error=true` menampilkan pesan token salah).

`POST /login` → verifikasi `token` vs `OPENCLAWN_AUTH_TOKEN` (constant-time). Salah →
redirect `/login?error=true`. Benar → set cookie `openclawn_session` (signed HMAC, 7 hari)
+ `openclawn_csrf` (dibaca template untuk field CSRF form), redirect ke `next` (divalidasi
harus path relatif — cegah open-redirect ke domain eksternal).

`POST /logout` → hapus kedua cookie, redirect `/login`. Butuh `csrf_token` valid seperti
form lain (tombol Sign out di sidebar, hanya tampil bila `auth_enabled`).

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

**Heartbeat SSE** (`_with_heartbeat`, membungkus `generate()` di `/chat/stream` & `/converse/stream`): selama agent diam lama (model lokal lambat, reasoning tanpa token, tool berjalan), tak ada frame terkirim → watchdog frontend menyangka "server not responding" padahal koneksi HIDUP. Wrapper me-race event berikutnya vs timeout `_HEARTBEAT_SEC` (10s); bila timeout menang, kirim komentar SSE `: ping\n\n` lalu tunggu lagi. Parser SSE MENGABAIKAN baris komentar (tak jadi frame data), tapi kedatangannya me-reset watchdog client (`readSSE` meneruskannya sebagai `'ping'`) & menjaga koneksi hangat (proxy tak memutus idle). **Tak ada reconnect** — koneksi tak pernah putus, jadi tak ada yang perlu disambung ulang. Watchdog frontend (`STALL_MS`=25s, > 2× interval) kini hanya menyala saat beberapa heartbeat berturut hilang (lambat sungguhan) dengan teks "masih bekerja" (bukan "server tidak merespons" yang menakutkan).

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

**Daftar permintaan approval yang menunggu keputusan.** Endpoint pendukung/introspeksi
(mis. dashboard eksternal) — Web UI chat TIDAK melakukan polling terpisah ke endpoint
ini (lihat catatan di bawah untuk bagaimana chat sesungguhnya menampilkan approval).

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

---

#### `GET /workdir/check`

**Validasi live folder kerja adaptif per-sesi (§ working directory adaptif).**

Query param:
- `path` — folder yang diketik user (boleh kosong)

Response:
```json
{"ok": true, "resolved": "/abs/path", "default": false}   // valid
{"ok": true, "resolved": null, "default": true}           // kosong → pakai default server
{"ok": false, "error": "Folder '...' tidak ditemukan atau tidak bisa diakses."}
```

Memakai `_validate_workdir` yang SAMA dengan jalur eksekusi (fail-closed) agar hasil UI konsisten dengan yang benar-benar dipakai. Dipanggil `chat.js` (debounced 350ms) saat user mengetik di `#workdir-input` → memberi umpan balik warna (border hijau valid / merah invalid) via kelas `.valid`/`.invalid` pada `.workdir-pick`, sehingga folder salah ketik ketahuan SEGERA, bukan gagal di tengah turn. GET → tidak butuh CSRF.

---

#### `POST /approve`

**User memutuskan approve atau reject untuk tool destruktif.**

Form data:
- `approval_id` — ID approval
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

**Bagaimana chat SESUNGGUHNYA menampilkan approval (§ chat approval UI):** sebelumnya
`GET /approvals` didokumentasikan sebagai "dipolling Web UI", tapi `chat.js` sama sekali
tidak pernah memanggilnya — setiap tool `requires_approval=True` selalu timeout diam-diam
setelah `approval_timeout_sec` karena tak ada tombol Approve/Reject di mana pun. Perbaikan:
`AgentLoop` kini men-generate `approval_id` **sebelum** memanggil `ApprovalGate.request()`
(yang blocking) dan meng-emit `AgentEvent(type="status", text="approval", approval_id=...)`
lewat SSE (`event: status` di `/chat/stream` & `/converse/stream`, field `approval_id`
disertakan di payload JSON). `chat.js` (`appendApprovalCard`) merender kartu dengan tombol
Approve/Reject yang langsung `POST /approve` dengan ID tersebut — begitu diklik, Future yang
sedang ditunggu `ApprovalGate.request()` ter-resolve dan stream lanjut, tanpa perlu menunggu
timeout. Kartu memisahkan `detail` (`tool_name(param)`) jadi nama tool + baris parameter
tersendiri (user melihat JELAS path/command yang akan dijalankan sebelum setuju), dengan
aksen amber berdenyut selama pending (`.approval-pending`, § `chat.css`) yang berhenti &
berubah warna sesuai keputusan. Endpoint `GET /approvals` tetap ada sebagai introspeksi
read-only, bukan jalur utama.

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
4. `ToolAudit(db).summary()` — statistik penggunaan tool

Context yang dikirim:
- `report` — list data per complexity label (total, corrections, correction_rate, avg_cost)
- `calibration` — dict `{total_events, has_enough_data, net_offset_delta, recommendations, current_offset, history}`
- `tool_stats` — list per tool `{tool_name, total, errors, timeouts, fail_rate, avg_latency_ms}`

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

Bar decay tiap baris menandai garis ambang arsip; fill berubah warna (kuning→merah) saat skill mendekati arsip. Halaman ini juga menampilkan **percobaan kristalisasi** (`crystallization_log`): status active/draft/duplicate, confidence, gap kritis, generator→evaluator, alasan (observability Inovasi 3).

---

#### `GET /conversations`

**Arsip percakapan multi-agent.**

Template: `web/templates/conversations.html`

Membaca 50 percakapan terakhir dari tabel `conversations` (diisi `ConversationOrchestrator._persist`). Tiap entri `<details>` menampilkan pattern, peserta, jumlah giliran, alasan akhir, biaya, dan transkrip penuh `[[role, content], ...]`. Read-only.

Context: `conversations` — list dict `{pattern, participants, initial_message, transcript, turns, end_reason, cost_usd, created_at}`.

---

#### `GET /workspace/download`

**Unduh file yang ditulis agent** (`?path=` relatif ke workspace). Dibatasi ke
`CONFIG.workspace_root` lewat guard yang sama dipakai tool file (`resolve_in_workspace`,
`infra/workspace.py`) — path traversal (`../`), path absolut di luar workspace, atau
symlink yang keluar workspace semuanya ditolak. Path tak ditemukan, di luar workspace,
atau menunjuk direktori → 404 seragam (tidak membedakan alasan ke client, agar tak
membocorkan struktur filesystem di luar workspace). Dipicu dari chip download di chat
(lihat `AgentEvent(type="file_created")` — `core/agent_loop.py` — dan `chat.js`
`appendFileDownload()`), muncul otomatis tiap kali tool penulis file (`file_write`,
`file_edit`, `file_append`, `apply_patch`, `doc_write`, `pdf_write`) sukses.

---

#### `GET /skills/export` & `POST /skills/import`

**Berbagi skill antar-instalasi** (skill packs, terinspirasi Multica). Lewat `core/skill_pack.py`.

`GET /skills/export?role=` → unduh skill `active` sebagai berkas Markdown (`Content-Disposition: attachment`); role tak dikenal → ekspor semua.

`POST /skills/import` → impor pack dari `pack_text` (tempel) atau `url`, opsional `target_role`. **Berlapis keamanan (§1):** SSRF guard (URL) → Shield scan → status **`draft`** (tak auto-masuk context, user aktifkan manual) → hash. Redirect `/skills?import_msg=...` dengan ringkasan. UI ada di `skills.html` (panel `<details>` ekspor/impor).

`POST /skills/apply-merge` → terapkan satu usulan merge `pending` (I1, gated `curation_auto=False` §8 default): winner menyerap konten sintesis, loser → `merged`. Form: `role`, `curation_id`. Redirect `/skills`. Panel "Curation" menampilkan tombol **Terapkan** untuk usulan `pending` terbaru.

`POST /skills/revert-merge` → batalkan merge skill yang **sudah diterapkan** (I1, `status='applied'`) untuk satu role: loser kembali `active`, winner ke konten/versi sebelum merge. Form: `role`. Redirect `/skills`. Panel "Curation" di `skills.html` menampilkan `curation_log` + tombol Batalkan untuk baris `applied` terbaru. `/metrics` menampilkan badge `auto-tune ON/OFF` (I4, `CONFIG.calibration_auto_apply`).

---

#### `GET /activity`

**Linimasa aksi agent** (terinspirasi Activity Timeline Multica). Template: `web/templates/activity.html`.

Agregasi read-only lintas tabel via `ActivityTimeline.recent(role)` (routing/tool/handoff/conversation/crystallize/blocker). Param `?role=` opsional memfokuskan satu peran (role tak dikenal → abaikan, tampil semua). Blocker terbuka (`agent_blockers.status='open'`) ditampilkan menonjol di banner atas, diurut severity. Read-only.

#### `POST /blockers/resolve`

Tandai blocker `resolved` (`agent_blockers.status`, set `resolved_at`). Form: `blocker_id`. Redirect `/activity`.

---

#### `GET /mcp` + `POST /mcp/add` · `/mcp/toggle` · `/mcp/delete`

**Kelola server MCP eksternal** (tool ekosistem Model Context Protocol). Template: `web/templates/mcp.html`.

`GET /mcp` → daftar server (`mcp_servers`) + tool yang ditemukan (`MCPRegistry.discovered_tools`). `POST /mcp/add` → tambah server (`name`, `transport` stdio|http, `command` dipisah spasi, atau `url`) lalu `load_all()` untuk discover segera. `toggle`/`delete` mengubah status & reload. **Keamanan (§1):** tool MCP selalu butuh approval; remote di-guard SSRF; role harus opt-in via `soul.toml` (`mcp__*`). Server dimuat saat lifespan startup (fail-safe).

---

#### `GET /autopilots` & `POST /autopilots`

**Kelola tugas agent terjadwal** (terinspirasi Autopilots Multica). Template: `web/templates/autopilots.html`.

`GET` menampilkan jadwal (`AutopilotStore.list_all`), riwayat run (`recent_runs`), dan proposal menunggu (`approval_log.decision='proposal:pending'` — aksi destruktif yang DIANTRI autopilot, bukan dieksekusi). `POST` membuat autopilot: form `name`, `role` (harus dikenal), `prompt`, `every` + `unit` (menit/jam/hari → detik). Validasi gagal → redirect tanpa membuat.

**Keamanan (§1, §17):** autopilot dijalankan `_run_autopilot` dengan `AgentConfig.autopilot=True` → tool butuh-approval tidak dieksekusi, diantri jadi proposal. Scheduler (`AutopilotScheduler`) start/stop di lifespan.

#### `POST /autopilots/toggle` & `POST /autopilots/delete`

Aktif/jeda (`set_enabled`) dan hapus (`delete`) autopilot. Form: `autopilot_id` (+ `enabled` untuk toggle). Redirect `/autopilots`.

---

#### `GET /router` & `POST /router`

**Editor peta tier→model.** Template: `web/templates/router.html`.

`GET` menampilkan 5 tier (TRIVIAL→CRITICAL) dengan dropdown model dari `KNOWN_MODELS`, preselect model aktif, + tanda `default` per tier. `POST` menyimpan: tiap tier dikirim sebagai field `tier_<key>` berformat `provider|model` → `RouterConfigStore.set_map()`; `action=reset` → `RouterConfigStore.reset()`. Redirect `/router?saved=true`.

Router tetap memutuskan TIER otomatis; halaman ini hanya menentukan MODEL tiap tier. Model offline → fallback chain. Beda dari `/settings` (yang memaksa SEMUA tier ke 1 model, mematikan router).

Context: `tiers` (list `{key, label, model, provider, is_default}`), `known_models`, `overridden`, `saved`.

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
- Bagian **Penggunaan Tool** (`tool_stats`): tabel per tool — dipakai, error, timeout, fail rate, avg latency

### `skills.html`

Template dashboard `/skills`. Menampilkan:
- Count chip active/draft/archived
- Tabel per skill: nama, role (role-dot), status, bar decay terproyeksi dengan garis ambang arsip, hari idle, use_count, confidence
- Tabel **Kristalisasi** (Inovasi 3): percobaan terakhir — status, confidence, gap kritis, generator→evaluator, alasan

### `conversations.html`

Template arsip `/conversations`. Daftar `<details>` per run: pattern + peserta + ringkasan (giliran/alasan/biaya/waktu) di summary, transkrip penuh (bubble per role, user dibedakan) di body.

### `router.html`

Template `/router`. Tabel 5 tier dengan dropdown model per tier (dari `KNOWN_MODELS`) + tombol Simpan/Reset. Tanda `default` bila tier masih memakai model bawaan.

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
- `http://localhost:8000/skills` — skill decay + kristalisasi dashboard
- `http://localhost:8000/conversations` — arsip percakapan multi-agent
- `http://localhost:8000/router` — editor peta tier→model
- `http://localhost:8000/settings` — override model
