# `security/` — Keamanan

Tiga komponen keamanan dengan tanggung jawab yang berbeda. **Pertahanan utama tetap container isolation** (`DockerSandbox`) — bukan Shield.

---

## `security/vault.py`

### Kelas: `Vault`

Menyimpan dan mengambil credential dari environment variable. **Credential tidak pernah masuk context/prompt LLM** — hanya diinjeksi saat outbound request.

**`__init__()`**  
Inisialisasi in-memory cache `_cache`.

**`get(key: str) → str`** *(async)*  
Ambil credential dengan key tertentu:
1. Cek cache in-memory
2. Jika tidak ada, baca dari `os.environ`
3. Jika tidak ada di environment, raise `ValueError`

> **Aturan:** Jangan pernah log nilai yang dikembalikan vault. Jangan print, jangan simpan ke DB, jangan masukkan ke string log.

**Contoh penggunaan:**
```python
api_key = await self.vault.get("ANTHROPIC_API_KEY")
headers = {"x-api-key": api_key, ...}
```

---

## `security/shield.py`

### Konstanta: `DANGER_PATTERNS`

Daftar pola regex yang menandakan upaya prompt injection atau jailbreak:
- `ignore (previous|all) instructions`
- `abaikan (instruksi|perintah) (sebelumnya|di atas)`
- `system prompt`
- `reveal your (instructions|prompt)`

### Kelas: `Shield`

**Lapisan kosmetik** — menangkap upaya jailbreak yang jelas. Bukan pertahanan utama (pertahanan utama = container isolation).

**`scan_input(text: str) → tuple[bool, str]`** *(staticmethod)*  
1. Normalisasi NFKD + encode ASCII — mencegah bypass via homoglyph (`ìgnore` → `ignore`)
2. Cocokkan dengan `DANGER_PATTERNS` (case-insensitive)
3. Return `(True, "")` jika aman, `(False, "Input ditolak: ...")` jika mencurigakan

Dipanggil di awal `agent_loop.run()` sebelum input masuk pipeline apapun.

---

## `security/approval.py`

### Dataclass: `PendingApproval`

Mewakili satu permintaan approval yang sedang menunggu keputusan user.

| Field | Keterangan |
|---|---|
| `approval_id` | UUID hex unik untuk permintaan ini |
| `session_id` | Sesi yang meminta approval |
| `tool_name` | Nama tool yang butuh disetujui |
| `tool_input` | Input tool sebagai dict |
| `future` | `asyncio.Future` yang di-resolve saat user klik approve/reject |

### Kelas: `ApprovalGate`

Human-in-the-loop (HITL) gate untuk tool destruktif.

**Arsitektur singleton:** `AgentLoop` dibuat baru tiap request, tapi `ApprovalGate` harus di-inject sebagai singleton dari level app (`web/main.py`) agar `resolve()` dari endpoint `/approve` bisa mencapai Future yang sama.

**`__init__(db, config)`**  
Inisialisasi dict `_pending` untuk track approval yang menunggu.

**`request(session_id, tool_name, tool_input) → bool`** *(async)*  
Alur lengkap permintaan approval:
1. Buat `PendingApproval` dengan UUID baru
2. Catat ke tabel `approval_log` dengan status `pending:{approval_id}`
3. Tunggu `asyncio.wait_for(future, timeout=approval_timeout_sec)`
4. Jika timeout → **fail-safe DENY** (`approved=False`, decision=`"timeout"`)
5. Update `approval_log` dengan keputusan final
6. Return `True` (approved) atau `False` (rejected/timeout)

Fail-safe DENY dipilih sesuai prinsip CLAUDE.md §1.1: keamanan dulu — tool destruktif tidak pernah jalan tanpa persetujuan eksplisit.

**`resolve(approval_id, approved) → bool`**  
Dipanggil dari endpoint `/approve` saat user klik tombol. Set result pada Future yang menunggu di `request()`. Return `True` jika approval_id valid dan berhasil di-resolve, `False` jika tidak ditemukan atau sudah selesai.

**`pending_list(session_id=None) → list[dict]`**  
Kembalikan daftar approval yang masih menunggu. Bisa difilter per sesi. Dipakai endpoint `/approvals` untuk polling dari Web UI.

**`_record_decision(approval_id, decision) → None`** *(async, private)*  
Update row `approval_log` dari `pending:{approval_id}` ke keputusan final.

---

## `security/question.py`

### Dataclass: `PendingQuestion`

Pertanyaan klarifikasi (`ask_user`) yang menunggu jawaban user dari Web UI: `question_id`, `session_id`, `question`, `future`.

### Kelas: `QuestionGate`

Analog `ApprovalGate` tapi untuk **pertanyaan terbuka** (bukan ya/tidak). Memberi tool `ask_user` kemampuan benar-benar bertanya ke user di tengah turn (menggantikan stub lama). Di-inject sebagai singleton dari `web/main.py` agar `resolve()` dari endpoint `/answer` mencapai Future yang sama.

**Ephemeral by design:** tidak ada tabel DB — jawaban klarifikasi tidak punya nilai audit seperti keputusan approval (CLAUDE.md §6). State hanya Future in-memory + registry per session.

**`ask(session_id, question) → str`** *(async)*  
Ajukan pertanyaan & tunggu jawaban. Timeout (`approval_timeout_sec`) → kembalikan `NO_ANSWER` (**fail-soft** — beda dari approval yang fail-safe DENY; pertanyaan tak dijawab tidak berbahaya, agent lanjut dengan asumsi).

**`resolve(question_id, answer) → bool`**  
Set jawaban pada Future berdasarkan `question_id`. Return `True` bila valid.

**`resolve_by_session(session_id, answer) → bool`**  
Resolve pertanyaan pending tertua (FIFO) untuk sebuah session — dipakai endpoint `/answer` agar frontend cukup kirim `session_id` tanpa melacak `question_id` (single-user, satu pertanyaan aktif per session pada satu waktu).

**`pending_list(session_id=None) → list[dict]`**  
Daftar pertanyaan yang masih menunggu jawaban.

> **Eksekusi `ask_user`** ditangani `AgentLoop._execute_tool`: bila tool = `ask_user`, ia memanggil `question_gate.ask()` (bukan `tool.execute()`), dan tool loop meng-emit `AgentEvent(type="status", text="question")` agar UI memunculkan kotak jawaban.

---

## Alur HITL End-to-End

```
User kirim pesan
    ↓
AgentLoop._execute_tool("code_run", {...})
    ↓
tool.requires_approval == True
    ↓
approval_gate.request(session_id, "code_run", input)
    [Future dibuat, request MENUNGGU]
    ↓
Web UI polling GET /approvals → dapat approval_id
    ↓
User klik Approve/Reject di Web UI
    ↓
POST /approve {approval_id=..., decision="approve"}
    ↓
approval_gate.resolve(approval_id, True)
    [Future di-resolve]
    ↓
request() return True
    ↓
tool.execute(input_data) dijalankan
```

Jika user tidak merespons dalam `approval_timeout_sec` (default 120 detik) → Future timeout → tool tidak jalan.

---

## Ringkasan Tanggung Jawab

| Komponen | Melindungi dari | Lapisan |
|---|---|---|
| `Vault` | Credential bocor ke prompt/log | Semua LLM call |
| `Shield` | Prompt injection yang jelas | Input user |
| `ApprovalGate` | Tool destruktif jalan tanpa izin | Tool execution |
| `QuestionGate` | (bukan keamanan) klarifikasi interaktif `ask_user` | Tool execution |
| `DockerSandbox` | Kode berbahaya akses host/network | **Pertahanan utama** |
