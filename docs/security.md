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

Daftar pola regex yang menandakan upaya prompt injection atau jailbreak (prompt-injection klasik, eksfiltrasi instruksi, mode jailbreak). Cosmetik — diperluas seiring pola umum, tapi tetap BUKAN pertahanan utama.

### Kelas: `Shield`

**Lapisan kosmetik** — menangkap upaya jailbreak yang jelas. Bukan pertahanan utama (pertahanan utama = container isolation).

**`scan_input(text: str) → tuple[bool, str]`** *(staticmethod)*  
1. Normalisasi NFKD + encode ASCII — mencegah bypass via homoglyph (`ìgnore` → `ignore`)
2. Cocokkan dengan `DANGER_PATTERNS` (case-insensitive)
3. Return `(True, "")` jika aman, `(False, "Input ditolak: ...")` jika mencurigakan

Dipakai oleh `PromptInjectionRail` (lihat Guardrails) — di awal `agent_loop.run()` input dijalankan lewat `GuardrailEngine`, bukan memanggil `Shield` langsung.

---

## `security/guardrails.py` — Guardrails (ala NeMo)

Rail input/output ringan, **terinspirasi arsitektur NVIDIA NeMo Guardrails** tanpa memakai paketnya (paket NeMo butuh LangChain + dependency berat → melanggar CLAUDE.md §6/§1.4/§8). Mengadopsi *konsep* rail, persis seperti `skill_scanner` meniru `nvidia/skillspector`. **Murni stdlib** (`re`, `dataclasses`) — extractable, tanpa DB (config dipisah ke `core/guardrails_config.py`).

### Model rail

| Stage | Kapan jalan | Rail bawaan |
|---|---|---|
| **INPUT** | sebelum pesan user masuk pipeline | `prompt_injection` |
| **OUTPUT** | saat finalisasi turn, sebelum disimpan ke history/memori | `prompt_leak`, `pii` |

> **Catatan jujur soal streaming:** token di-stream real-time ke UI, jadi output rail tidak bisa "menarik kembali" token yang sudah tampil. Yang dilakukan: memeriksa `turn.content` LENGKAP → **meredaksi/memblokir sebelum disimpan** ke history & L1/L4, lalu memancarkan event `guardrail` ke UI. Deteksi + redaksi-penyimpanan tetap bernilai (PII tak bocor ke memori; audit mencatat).

### Enum & dataclass

- `RailStage` — `INPUT` / `OUTPUT`
- `RailAction` — `ALLOW` (lolos) / `REDACT` (teks dimodifikasi) / `BLOCK` (tolak total)
- `RailResult` — hasil satu rail: `rail`, `action`, `text`, `reason`, `findings`, properti `triggered`
- `GuardrailOutcome` — hasil agregat satu stage: `text` (final), `blocked`, `block_reason`, `results`, properti `modified`

### Rail bawaan

| Rail | Stage | Aksi | Deteksi |
|---|---|---|---|
| `PromptInjectionRail` | input | BLOCK | bungkus `Shield.scan_input` (DRY) |
| `PromptLeakRail` | output | BLOCK | respons membocorkan system-prompt/peran internal |
| `PIIRail` | output | REDACT | email, kartu kredit, kunci API → `[REDACTED]` |

### Kelas: `GuardrailEngine`

**`__init__(enabled=None)`** — `enabled`: peta `nama_rail → bool`. `None` → `DEFAULT_ENABLED` (semua aktif).

**`run(stage, text) → GuardrailOutcome`** — jalankan rail aktif untuk stage secara berurutan. `BLOCK` menghentikan rantai; `REDACT` meneruskan teks teredaksi ke rail berikutnya. Output yang BLOCK → teks diganti `BLOCKED_OUTPUT_MESSAGE` (teks asli tak bocor).

**`check_input(text)` / `check_output(text)`** — shortcut untuk `run(INPUT/OUTPUT, text)`.

Konstanta: `BUILTIN_RAILS` (nama→kelas), `DEFAULT_ENABLED`, `BLOCKED_OUTPUT_MESSAGE`.

---

## `core/guardrails_config.py` — on/off per rail

`GuardrailConfigStore` menyimpan peta `nama_rail → bool` sebagai satu key JSON di `app_settings` (pola sama `RouterConfigStore`). DB-bound (§1.6) — dipisah dari engine agar `guardrails.py` tetap murni stdlib.

| Method | Keterangan |
|---|---|
| `get_enabled() → dict[str,bool]` *(async)* | Peta aktif. Tanpa config → semua aktif. Rail hilang dari config dianggap **aktif** (fail-safe default-on). Korup → semua aktif. |
| `set_enabled(mapping) → dict` *(async)* | Simpan on/off; hanya rail dikenal disimpan |
| `reset() → None` *(async)* | Hapus config → semua rail aktif lagi |

Dibaca `AgentLoop` tiap turn untuk membangun `GuardrailEngine(enabled=...)` — perubahan UI langsung berlaku tanpa restart.

---

## `security/skill_scanner.py`

Pemeriksa keamanan untuk **skill yang diimpor dari luar** (skill packs). Terinspirasi `nvidia/skillspector`: skill pack = konten TAK-TEPERCAYA, jadi diperiksa SEBELUM masuk DB. Lebih dalam dari `Shield` (yang hanya regex prompt-injection) — menangkap kode/eksfiltrasi yang dibawa skill. Murni stdlib (`ast`+`re`), tanpa dependency (§6). **Selalu aktif** pada impor — keamanan bukan optimasi, tak bisa dimatikan dari UI.

Dua lapis:
1. **AST** (`ast.walk`) pada blok kode berpagar (```` ```python ````): `exec`/`eval`/`compile`/`__import__`, `os.system`/`os.popen`, `subprocess.*`, `shutil.rmtree`, `open(...,'w')`. Blok yang tak parse sebagai Python → di-skip diam (banyak skill = prosa).
2. **Pola leksikal**: eksfiltrasi shell (`curl|wget … | sh`), `curl` POST, path kredensial (`~/.ssh`, `id_rsa`, `.aws/credentials`), URL dengan kredensial inline, endpoint metadata cloud (`169.254.169.254`), `eval(input())`, blob base64 panjang.

### Dataclass: `ScanResult`

| Field | Keterangan |
|---|---|
| `score` | 0–100, akumulasi severity per temuan (di-clamp 100) |
| `verdict` | `clean` (<25) / `flag` (25–49) / `reject` (≥50) |
| `findings` | list label temuan (mis. `ast call:exec`, `pattern shell_exfil`) |
| `blocked` *(property)* | `True` bila `verdict == "reject"` |

Ambang: satu temuan **kritis** (exec/subprocess/`curl\|sh` = 50) cukup untuk reject sendirian — eksekusi kode arbitrer tak butuh bukti kedua. Temuan sedang (15) baru reject bila menumpuk.

**`scan_skill(name, content) → ScanResult`**  
Pindai satu skill. **Tak pernah raise** (input eksternal): kegagalan analisis = fail-safe ke temuan terkumpul, bukan diam-diam meloloskan.

**Jalur impor** (`core/skill_pack.py`): `reject` → skill DITOLAK total (tak masuk DB, keputusan owner §1); `flag` → tetap impor sebagai draft tapi dicatat di `flagged` & log; `clean` → draft normal. Berlaku untuk impor file maupun URL (defense-in-depth).

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

**`request(session_id, tool_name, tool_input, approval_id=None) → bool`** *(async)*  
Alur lengkap permintaan approval:
1. Buat `PendingApproval` — pakai `approval_id` bila diberikan, kalau tidak generate UUID baru
2. Catat ke tabel `approval_log` dengan `decision="pending"` dan `approval_id` di KOLOM SENDIRI (§ Human Approval Pipeline, TODO.md Prioritas 2 — sebelumnya `approval_id` hanya tersirat sebagai substring `pending:{id}` di kolom `decision`, hilang begitu langkah 5 menimpanya jadi keputusan final; sekarang tetap query-able lintas status via `GET /approval/{approval_id}`, `docs/web.md`)
3. Tunggu `asyncio.wait_for(future, timeout=approval_timeout_sec)`
4. Jika timeout → **fail-safe DENY** (`approved=False`, decision=`"timeout"`)
5. Update `approval_log` dengan keputusan final (dicari via kolom `approval_id`, bukan lagi pola string `decision=pending:{id}`)
6. Return `True` (approved) atau `False` (rejected/timeout)

Fail-safe DENY dipilih sesuai prinsip CLAUDE.md §1.1: keamanan dulu — tool destruktif tidak pernah jalan tanpa persetujuan eksplisit.

Parameter `approval_id` opsional (§ chat approval UI) — `AgentLoop._run_tool_loop` sekarang
generate ID ini SEBELUM memanggil `request()` (yang blocking) dan meng-emit
`AgentEvent(type="status", text="approval", approval_id=...)` ke Web UI lebih dulu, agar
tombol Approve/Reject bisa dipasang dengan ID yang benar sementara `request()` masih
menunggu. Default `None` → generate seperti sebelumnya (tak ada perubahan perilaku untuk
caller yang tidak memberi ID, mis. test lama).

**`resolve(approval_id, approved) → bool`**  
Dipanggil dari endpoint `/approve` saat user klik tombol. Set result pada Future yang menunggu di `request()`. Return `True` jika approval_id valid dan berhasil di-resolve, `False` jika tidak ditemukan atau sudah selesai.

**`auto_approve(session_id, tool_name, tool_input) → bool`** *(async)*  
Trust mode per-sesi (§ user request otonomi): tool YANG BUTUH APPROVAL tetap DIEKSEKUSI sungguhan, tapi tanpa Future/blocking — langsung catat ke `approval_log` dengan `decision="auto:trust_mode"` (berbeda dari `"approved"` manual, agar audit trail membedakan keputusan manusia vs toggle) lalu return `True`. Beda dari `queue_proposal`: manusia SEDANG hadir di sesi chat aktif (bukan autopilot tanpa manusia), hanya melewati klik. Caller (`AgentLoop._execute_tool`) yang memutuskan tool mana boleh lewat sini — `code_run` TIDAK PERNAH, berapa pun trust mode-nya (CLAUDE.md §1, lihat `core/agent_loop.py` § `_TRUST_MODE_EXEMPT`).

**`pending_list(session_id=None) → list[dict]`**  
Kembalikan daftar approval yang masih menunggu. Bisa difilter per sesi. Endpoint
introspeksi read-only (`GET /approvals`) — Web UI chat TIDAK memakai polling ke sini;
lihat `docs/web.md` § `POST /approve` untuk bagaimana chat sesungguhnya menampilkan
approval (via event SSE `status.approval_id`, bukan polling terpisah).

**`_record_decision(approval_id, decision) → None`** *(async, private)*  
Update row `approval_log` yang `approval_id`-nya cocok DAN `decision='pending'`, jadi keputusan final.

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

## `security/auth.py` — Self-host auth (§P0 production-readiness)

**Single-user shared-secret login**, bukan sistem akun multi-user. Menjawab
"apakah orang ini tahu `OPENCLAWN_AUTH_TOKEN`", bukan lebih dari itu. Murni
stdlib (`hmac` + `secrets`) — sengaja tidak memakai `itsdangerous`/`SessionMiddleware`
Starlette agar tidak menambah dependency baru (§7) tanpa persetujuan eksplisit.

**Fail-open by default:** `CONFIG.auth_active` False (baik `auth_token` MAUPUN
OIDC — lihat `security/oidc.py` di bawah — keduanya kosong, default) → seluruh
middleware auth di `web/main.py` di-skip, perilaku lama (tanpa login) tetap
jalan — aman untuk localhost dev. Diaktifkan bila `OPENCLAWN_AUTH_TOKEN` ATAU
OIDC diisi di `.env` (self-host di VPS publik, lihat README § Scope & Production Posture).

**Session secret independen dari `auth_token`** (TODO.md § Prioritas 5):
`create_session_token`/`verify_session_token` menerima `CONFIG.session_secret`,
BUKAN `auth_token` langsung — sebelum OIDC ada, keduanya identik (aman, hanya
SATU deployment shared-secret yang tahu nilainya). Dengan OIDC, operator bisa
memilih login HANYA lewat provider (tanpa `auth_token` sama sekali) — di situ
`auth_token` kosong tak bisa jadi secret HMAC. `session_secret` resolve:
`auth_token` (bila diisi) → `OPENCLAWN_SESSION_SECRET` eksplisit → fallback acak
saat boot (aman, tapi restart me-logout semua sesi — operator OIDC-only yang
ingin sesi bertahan lintas-restart HARUS mengisi `OPENCLAWN_SESSION_SECRET`).

**`create_session_token(secret) → str`**  
Token sesi `{timestamp}.{hmac_hex}` ditandatangani HMAC-SHA256. Dipanggil saat login sukses.

**`verify_session_token(token, secret, max_age_sec=SESSION_MAX_AGE_SEC) → bool`**  
Verifikasi signature (constant-time via `hmac.compare_digest`, cegah timing attack) +
expiry (`max_age_sec`, default `SESSION_MAX_AGE_SEC` = 7 hari). Token tanpa titik,
signature tak cocok, atau kedaluwarsa/masa depan → ditolak. Parameter `max_age_sec`
memungkinkan pemanggil (middleware) memakai batas lebih ketat dari absolute expiry —
dasar mekanisme idle timeout di bawah.

**`verify_login_token(candidate, secret) → bool`**  
Bandingkan password yang diketik user vs `OPENCLAWN_AUTH_TOKEN`, constant-time.

**`generate_csrf_token() → str`**  
Token acak (`secrets.token_urlsafe`) disimpan di cookie terpisah (`openclawn_csrf`,
`httponly=False` agar terbaca Jinja) + disuntik ke tiap form POST.

**`is_public_path(path) → bool`**  
`/health`, `/login`, `/static/*` selalu bisa diakses tanpa sesi (monitoring, aset
halaman login itu sendiri, dan login flow).

Diintegrasikan di `web/main.py` (`auth_and_csrf_middleware`): cek sesi → redirect
`/login` (GET) atau 401 JSON (non-GET) bila tak valid; lalu cek CSRF untuk POST
form biasa (endpoint SSE/fetch JS di `_CSRF_EXEMPT_PATHS` — sudah dilindungi
cookie auth + `SameSite=lax`, tak realistis membawa token form).

**Idle timeout (opt-in, `CONFIG.idle_timeout_sec`, TODO.md § Prioritas 1.5):**
token stateless hanya punya `ts` = waktu LOGIN, bukan waktu aktivitas terakhir —
tidak ada tabel sesi di server untuk melacak "last seen". Saat `idle_timeout_sec`
diisi, middleware melakukan dua hal tambahan:
1. Memvalidasi sesi dengan `max_age_sec=min(idle_timeout_sec, SESSION_MAX_AGE_SEC)`
   alih-alih absolute expiry biasa — token yang lebih tua dari jendela idle ditolak
   walau belum mencapai 7 hari.
2. Menerbitkan ULANG cookie sesi (`ts` baru) di response tiap request valid —
   efektif menjadikan `ts` "waktu aktivitas terakhir" sambil tetap stateless
   (tidak ada state baru di DB, hanya cookie yang di-refresh oleh browser).

Default `None` (OFF) → kedua langkah di atas di-skip sepenuhnya, perilaku lama
(hanya absolute expiry 7 hari) tak berubah.

---

## `security/oidc.py` — OAuth2/OIDC login (TODO.md § Prioritas 5)

Mode auth **TAMBAHAN** di samping shared-secret di atas, bukan penggantinya —
operator pilih SATU provider generik yang kompatibel Google/Microsoft/Okta/dsb
via discovery document standar (`{issuer}/.well-known/openid-configuration`),
BUKAN integrasi vendor-spesifik. `authlib` (httpx-based, konsisten stack) untuk
JWKS verification via `joserfc` — bukan implementasi JWT manual sendiri (risiko
bug keamanan lebih tinggi ketimbang library teraudit). Dependency baru disetujui
owner secara eksplisit; OIDC adalah protokol terbuka (seperti MCP), bukan SDK
vendor-LLM, jadi tak melanggar prinsip "no SDK Anthropic/OpenAI" (§1.6 — yang
dilarang khusus SDK vendor-LLM).

Alur (Authorization Code + state/nonce, TANPA PKCE — client_secret confidential
client cukup untuk server-side self-host; PKCE penting untuk public client
seperti SPA/mobile yang tak bisa simpan secret):

1. `GET /login/oidc` → `build_authorize_url()` → redirect ke provider dengan
   `state` (anti-CSRF) dan `nonce` (anti-replay ID token) acak, disimpan di
   cookie sementara `openclawn_oidc_state`/`openclawn_oidc_nonce` (httponly,
   umur 10 menit).
2. Provider redirect balik ke `GET /auth/callback?code=...&state=...`.
3. `exchange_code()` — tukar `code` → `id_token` mentah (network, POST ke
   `token_endpoint`).
4. `verify_id_token()` — verifikasi signature (JWKS provider, cache in-process
   TTL 1 jam) + klaim standar (`iss`/`aud`/`exp`/`nonce`) SEBELUM dipercaya.

**Fail-closed, BUKAN fail-open:** gagal di titik manapun (discovery/JWKS/exchange
network error, signature tak valid, `iss`/`aud`/`exp`/`nonce` tak cocok) →
`OIDCError`, login DITOLAK (redirect `/login?error=true`) — beda dari
`auth_token` kosong yang sengaja fail-open (desain opt-in lama). OIDC yang SUDAH
dikonfigurasi harus verifikasi ketat, tanpa pengecualian.

### Dataclass: `OIDCClaims`

`subject`, `email`, `name` — klaim ID token yang relevan setelah verifikasi berhasil.

### Fungsi

**`generate_state() → str`** / **`generate_nonce() → str`**  
Token acak (`secrets.token_urlsafe(32)`) untuk anti-CSRF (state) dan anti-replay (nonce).

**`build_authorize_url(issuer, client_id, redirect_uri, state, nonce) → str`** *(async)*  
Ambil discovery document (cache TTL 1 jam), susun URL `authorization_endpoint` +
`response_type=code&scope=openid email profile&state=...&nonce=...`.

**`exchange_code(issuer, client_id, client_secret, redirect_uri, code) → str`** *(async)*  
POST ke `token_endpoint`, return `id_token` MENTAH (belum diverifikasi) — caller
WAJIB memanggil `verify_id_token()` sebelum mempercayai isinya.

**`verify_id_token(issuer, client_id, id_token, expected_nonce) → OIDCClaims`** *(async)*  
Verifikasi signature via JWKS provider (`joserfc.jwt.decode`, algoritma RS256/ES256)
+ klaim `iss`/`aud`/`exp`/`nonce`/`sub`. Gagal di titik manapun → `OIDCError`.

Setelah verifikasi sukses, sesi yang diterbitkan (`web/main.py::_issue_session_cookies`)
SAMA PERSIS dengan shared-secret — OIDC hanya mengganti CARA membuktikan identitas
di titik login, bukan mekanisme sesi setelahnya. Tetap single-user secara internal
(§7): OIDC memverifikasi SIAPA yang login, bukan membuka multi-akun/RBAC (RBAC per
tenant adalah sub-item Prioritas 5 terpisah, belum dikerjakan).

Config: `OPENCLAWN_OIDC_ISSUER`, `OPENCLAWN_OIDC_CLIENT_ID`,
`OPENCLAWN_OIDC_CLIENT_SECRET`, `OPENCLAWN_OIDC_REDIRECT_BASE` (default
`http://localhost:8000`, HARUS diisi eksplisit untuk self-host di belakang
reverse proxy/domain kustom — tak bisa diasumsikan dari request).

---

## `security/rate_limit.py` — Rate limiting (§P0 production-readiness)

**Sliding window in-memory**, single-process — cukup untuk single-user (§7),
tak butuh Redis/dependency eksternal. Membatasi `/chat/stream` &
`/converse/stream` (default 20 request/60 detik per key) agar biaya LLM tak
tak-terkendali & mencegah DoS sederhana saat self-host di VPS publik.

### Kelas: `RateLimiter`

**`allow(key: str) → bool`**  
True bila request boleh lanjut; mencatat hit HANYA bila diizinkan (hit yang
ditolak tak ikut disimpan, agar retry setelah window lewat tak ikut diblokir).
`key` = session cookie auth (bukan app `session_id` — satu user dgn banyak tab
tetap dibatasi bersama), fallback client IP bila auth nonaktif.

**`remaining(key: str) → int`**  
Sisa kuota di window saat ini.

State in-memory murni — reset otomatis saat restart proses (dapat diterima
untuk single-user, tak perlu persisten).

---

## `security/policy_engine.py` — Policy Engine (TODO.md § Prioritas 3)

Lapisan kondisi **TAMBAHAN** di atas allow-list (`soul.toml [tools] allowed`)
dan approval statis (`Tool.requires_approval`) — TIDAK menggantikan keduanya.
Kondisi berbasis nested dict/TOML (BUKAN DSL string/`eval()`) — keputusan
desain sadar: parser ekspresi kustom menambah permukaan bug/kerentanan lebih
mahal diverifikasi dibanding operator tetap per tipe field. Konsisten prinsip
minimalis CLAUDE.md §8.

### Dataclass: `PolicyDecision`

`action: str` (`"allow"` / `"deny"` / `"require_approval"`), `reason: str`.

### Kelas: `PolicyEngine`

**`__init__(policy_cfg: dict)`**  
`policy_cfg` = `soul["policy"]` (dict kosong `{}` bila role tidak punya section `[policy]` sama sekali — semua tool ALLOW default, perilaku lama tak berubah).

**`evaluate(tool_name, tool_input) → PolicyDecision`**  
Cek `deny_if` dulu (OR semantics — kondisi PERTAMA yang match langsung `deny`), baru `approval_required_if`. `deny_if` SELALU menang atas `approval_required_if` bila keduanya match untuk tool yang sama (fail-safe: penolakan > permintaan approval, CLAUDE.md §1). Field yang dicek kondisi tapi tidak ada di `tool_input`, atau operator tak dikenal (typo config) → kondisi dianggap TIDAK match, BUKAN crash.

Operator yang didukung: `prefix`, `not_prefix`, `contains`, `eq`, `gt`, `gte`, `lt`, `lte`, dan `always` (match tanpa perlu field di `tool_input` sama sekali — dipakai `infra/manifest.py` untuk `approval_required_if` tanpa kondisi spesifik).

### Skema `soul.toml`

```toml
[policy.file_write]
deny_if = [{ field = "path", op = "prefix", value = "/etc" }]

[policy.http_request]
approval_required_if = [{ field = "url", op = "not_prefix", value = "https://api.internal" }]
```

### Integrasi `core/agent_loop.py`

Dievaluasi di **DUA titik** (defense-in-depth, pola sama `_TRUST_MODE_EXEMPT`):
1. `_run_tool_loop` — SEBELUM status UI di-emit & `bypass_approval` dihitung, agar tool yang `requires_approval=False` statis (mis. `shell_run`) tapi dipaksa approval oleh policy tampil sebagai kartu approval, BUKAN chip "tool" biasa.
2. `_execute_tool` — SEBELUM approval/eksekusi. `deny` → tolak SEBELUM approval sempat dipanggil sama sekali. `require_approval` → paksa masuk jalur `tool.requires_approval or policy_forces_approval`, DAN `bypass_approval` (trust mode) dipaksa `False` bila `policy_forces_approval` — **keputusan desain eksplisit**: policy adalah lapisan keamanan yang lebih kuat daripada preferensi otonomi sesi; kalau trust mode bisa melewatinya, policy jadi tak berarti apa-apa saat trust mode aktif. Dicek independen di kedua titik (bukan caller mempercayakan seluruhnya ke satu perhitungan) agar bug di satu titik tak membuka celah bypass.

---

## Ringkasan Tanggung Jawab

| Komponen | Melindungi dari | Lapisan |
|---|---|---|
| `Vault` | Credential bocor ke prompt/log | Semua LLM call |
| `GuardrailEngine` (input) | Prompt injection di input user | Input rail (ala NeMo) |
| `GuardrailEngine` (output) | Kebocoran system-prompt & PII di respons | Output rail (ala NeMo) |
| `Shield` | Prompt injection yang jelas (dipakai oleh input rail) | Input user |
| `skill_scanner` | Skill impor membawa kode berbahaya/eksfiltrasi | Impor skill pack (file/URL) |
| `ApprovalGate` | Tool destruktif jalan tanpa izin | Tool execution |
| `PolicyEngine` | Kondisi spesifik tool (path/domain/nilai) yang allow-list statis tak bisa tangkap | Tool execution (sebelum approval) |
| `QuestionGate` | (bukan keamanan) klarifikasi interaktif `ask_user` | Tool execution |
| `security/auth.py` | Akses tanpa login saat self-host publik (opt-in) | Semua route (kecuali `/health`, `/login`, `/login/oidc`, `/auth/callback`, `/static/*`) |
| `security/oidc.py` | Login via SSO enterprise (Google/Microsoft/Okta/dsb), opt-in | `/login/oidc`, `/auth/callback` |
| `security/rate_limit.py` | Biaya LLM tak terkendali / DoS sederhana | `/chat/stream`, `/converse/stream` |
| `DockerSandbox` | Kode berbahaya akses host/network | **Pertahanan utama** |
