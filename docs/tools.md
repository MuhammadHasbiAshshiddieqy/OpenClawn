# `tools/` — Tool yang Bisa Dipanggil Agent

Tool adalah aksi konkret yang bisa dilakukan agent di dunia nyata: baca file, tulis file, fetch URL, jalankan kode. Setiap tool punya schema JSON yang dikirim ke LLM.

---

## `tools/base.py`

### Abstract class: `Tool`

Base class untuk semua tool.

| Atribut/Method | Keterangan |
|---|---|
| `name: str` | Nama tool (dipakai sebagai key di `TOOL_REGISTRY`) |
| `requires_approval: bool` | Default `False`. Set `True` untuk tool destruktif |
| `execute(input_data, vault) → dict` | Eksekusi tool, return dict hasil |
| `schema() → dict` | Return JSON schema tool untuk dikirim ke LLM |

---

## `tools/__init__.py`

### `TOOL_REGISTRY`

Registry global semua tool yang tersedia:

```python
TOOL_REGISTRY = {
    # filesystem (workspace-bounded)
    "file_read":    FileReadTool(),
    "file_write":   FileWriteTool(),
    "file_edit":    FileEditTool(),
    "file_append":  FileAppendTool(),
    "apply_patch":  ApplyPatchTool(),
    "list_dir":     ListDirTool(),
    "glob":         GlobTool(),
    "grep":         GrepTool(),
    "pdf_read":     PdfReadTool(),
    # eksekusi (sandboxed)
    "shell_run":    ShellRunTool(),
    "code_run":     CodeRunTool(),
    # akses luar
    "web_fetch":    WebFetchTool(),
    "web_search":   WebSearchTool(),
    "http_request": HttpRequestTool(),
    # data & memori
    "db_query":     DbQueryTool(),
    "memory_search": MemorySearchTool(),
    "json_query":   JsonQueryTool(),
    # interaksi
    "ask_user":     AskUserTool(),
}
```

`AgentLoop` mengakses registry ini untuk lookup dan schema generation. Tool menerima `execute(input_data, vault, db=None)` — `db` (DatabaseManager) hanya dipakai `db_query`/`memory_search`, tool lain mengabaikannya.

> **Workspace sandbox (keamanan #1).** Semua tool filesystem (`file_read`, `file_write`,
> `file_edit`, `file_append`, `apply_patch`, `list_dir`, `glob`, `grep`, `pdf_read`) dibatasi ke `CONFIG.workspace_root` lewat
> `infra/workspace.py::resolve_in_workspace()`. Path yang keluar (lewat `..`, path absolut,
> atau symlink) ditolak dengan `{"error": "...di luar workspace..."}`. Set root via
> env `OPENCLAWN_WORKSPACE` (default `.`).

---

## `tools/file_ops.py`

### `FileReadTool`

Baca isi file dari filesystem.

- `requires_approval = False`
- Input: `{"path": "..."}` — path file yang dibaca
- Output sukses: `{"content": "..."}` — isi file (maks 10.000 karakter)
- Output error: `{"error": "..."}` — pesan error jika file tidak ditemukan atau permission denied

### `FileWriteTool`

Tulis (atau timpa) seluruh isi file. **Destruktif** → butuh approval. Membuat folder induk bila perlu (masih dalam workspace).

- `requires_approval = True`
- Input: `{"path": "...", "content": "..."}`
- Output sukses: `{"ok": true, "path": "...", "bytes": N}`
- Output error: `{"error": "..."}` jika permission denied atau path di luar workspace

### `FileEditTool`

Edit parsial: ganti `old_string` → `new_string` di file yang sudah ada. Lebih hemat token & aman dari `file_write` untuk perubahan kecil. **Destruktif** → butuh approval.

- `requires_approval = True`
- Input: `{"path": "...", "old_string": "...", "new_string": "...", "replace_all": false}`
- `old_string` harus cocok **persis** & **unik** (muncul >1× tanpa `replace_all` → error)
- Output sukses: `{"ok": true, "path": "...", "replacements": N}`
- Output error: `{"error": "..."}` jika string tidak ditemukan / tidak unik / di luar workspace

### `FileAppendTool`

Tambah konten ke **akhir** file tanpa menimpa (buat bila belum ada). **Destruktif** → butuh approval.

- `requires_approval = True`
- Input: `{"path": "...", "content": "..."}`
- Output sukses: `{"ok": true, "path": "...", "appended": N}`

### `ApplyPatchTool`

Multi-edit **atomik** pada satu file: list `{old_string, new_string}`. Bila satu edit gagal cocok/tidak unik, **tidak ada** perubahan ditulis (file tidak setengah ter-edit). **Destruktif** → butuh approval.

- `requires_approval = True`
- Input: `{"path": "...", "edits": [{"old_string": "...", "new_string": "..."}, ...]}`
- Output sukses: `{"ok": true, "path": "...", "edits_applied": N}`
- Output error: `{"error": "edit #k: ..."}` — seluruh patch dibatalkan

---

## `tools/document.py`

### `PdfReadTool`

Ekstrak teks dari PDF dalam workspace (pakai `pypdf`). Read-only, tanpa approval.

- `requires_approval = False`
- Input: `{"path": "...", "page": <opsional, 1-indexed>}`
- Output: `{"pages": N, "text": "...", "truncated": bool}`
- Output error: `{"error": "..."}` jika file tidak ada / di luar workspace / gagal parse

---

## `tools/data.py`

### `DbQueryTool`

Query SQL **SELECT-only** ke DB internal (memori/skill/audit). Menolak INSERT/UPDATE/DELETE/DROP/dll & multi-statement. **Butuh approval** (akses state internal).

- `requires_approval = True`
- Input: `{"sql": "SELECT ..."}`
- Output: `{"rows": [...], "count": N, "truncated": bool}` (maks 100 baris)
- Output error: `{"error": "..."}` jika bukan SELECT, ada `;`, keyword tulis, atau query gagal
- `db` di-inject dari `AgentLoop`; tanpa `db` → error

### `MemorySearchTool`

Cari di memori agent (`skills`, `memory_l1`, `memory_l2`) via LIKE. Read-only, tanpa approval.

- `requires_approval = False`
- Input: `{"query": "...", "table": "skills|memory_l1|memory_l2"}`
- Output: `{"table": "...", "results": [...], "count": N}`
- Tabel di luar allowlist → error (tidak bisa baca `approval_log`/`routing_events` dari sini)

### `JsonQueryTool`

Ekstrak nilai dari JSON via dot-path (stdlib). Read-only, tanpa approval.

- `requires_approval = False`
- Input: `{"json": <string|object>, "path": "a.b.0.c"}` (path kosong = seluruh data)
- Output: `{"value": ...}`
- Output error: `{"error": "..."}` jika JSON tidak valid / path tidak ditemukan

---

## `tools/search.py`

### `GlobTool`

Cari file berdasarkan pola glob dalam workspace. Melewati `.git`, `node_modules`, `.venv`, `__pycache__`, dll.

- `requires_approval = False`
- Input: `{"pattern": "**/*.py", "path": "<subfolder opsional>"}`
- Output: `{"matches": ["rel/path.py", ...], "count": N}` (maks 200)

### `GrepTool`

Cari teks/regex di dalam isi file pada workspace.

- `requires_approval = False`
- Input: `{"pattern": "<regex>", "path": "<subfolder opsional>"}`
- Output: `{"matches": [{"file","line","text"}], "count": N, "truncated": bool}` (maks 100)
- Output error: `{"error": "..."}` jika regex tidak valid

---

## `tools/web.py`

### `WebFetchTool`

Fetch konten dari URL via HTTP GET.

- `requires_approval = False`
- Input: `{"url": "..."}`
- Output sukses: `{"status": 200, "content": "..."}` — konten teks (maks 5.000 karakter)
- Output error: `{"error": "..."}` jika HTTP error

Timeout 30 detik, ikut redirect otomatis.

### `WebSearchTool`

Cari di web via Tavily API. API key (`TAVILY_API_KEY`) diambil lewat **Vault** saat outbound — tidak pernah masuk prompt/context (§1.2).

- `requires_approval = False`
- Input: `{"query": "...", "max_results": 5}`
- Output: `{"query": "...", "results": [{"title","url","snippet"}], "answer": "..."}`
- Output error: `{"error": "...TAVILY_API_KEY..."}` jika key tidak ada (gagal anggun)

### `HttpRequestTool`

HTTP request generik (GET/POST/PUT/PATCH/DELETE) ke API eksternal. **Destruktif** → butuh approval.

- `requires_approval = True`
- Input: `{"url": "https://...", "method": "GET", "headers": {...}, "body": ...}`
- Kredensial: nilai header berformat `"vault:NAMA_KEY"` di-resolve dari Vault (jangan tulis API key langsung)
- Output sukses: `{"status": N, "body": "...", "truncated": bool}`
- Output error: `{"error": "..."}` jika URL/method tidak valid atau kredensial vault hilang

---

## `tools/interaction.py`

### `AskUserTool`

Tool untuk bertanya ke user. **Saat ini stub** — belum diimplementasi sebagai SSE interaktif.

- `requires_approval = False`
- Input: `{"question": "..."}`
- Output: `{"answer": "[stub] pertanyaan tertunda: ..."}` — placeholder sampai UI interaktif siap

---

## `tools/code.py`

### `CodeRunTool`

Jalankan kode Python dalam Docker sandbox yang terisolasi. **Selalu butuh approval.**

- `requires_approval = True` (selalu)
- Input: `{"code": "..."}`
- Output: lihat `DockerSandbox.run_python()`

Delegasi seluruh eksekusi ke `DockerSandbox` — tidak ada `exec()`, `eval()`, atau `subprocess` langsung ke host.

---

## `tools/sandbox.py`

### Konstanta

| Konstanta | Nilai | Keterangan |
|---|---|---|
| `SANDBOX_IMAGE` | `openclawn-sandbox:latest` | Docker image yang dipakai |
| `SANDBOX_TIMEOUT_SEC` | `30` | Timeout keras eksekusi kode (detik) |
| `SANDBOX_MEM_LIMIT` | `"256m"` | Batas memori container |
| `SANDBOX_CPU_LIMIT` | `"0.5"` | Batas CPU container (50% satu core) |

### Kelas: `DockerSandbox`

**`run_python(code: str) → dict`** *(async)*  
Jalankan kode Python dalam container Docker yang terisolasi penuh.

Implementasi:
1. Buat temporary directory
2. Tulis kode ke `script.py` dalam temp dir
3. Jalankan `docker run` dengan flag keamanan ketat
4. Tunggu selesai dengan timeout ganda (container timeout + asyncio timeout)
5. Return stdout, stderr, dan exit code

**Flag Docker yang dipakai:**

| Flag | Nilai | Tujuan |
|---|---|---|
| `--network none` | — | Tidak ada akses internet |
| `--memory` | `256m` | Batas memori |
| `--cpus` | `0.5` | Batas CPU |
| `--read-only` | — | Filesystem read-only |
| `--tmpfs /tmp` | `rw,size=64m` | Satu-satunya area writable (ephemeral) |
| `-v {workdir}:/work:ro` | — | Mount script sebagai read-only |
| `--user nobody` | — | Non-root user |
| `--security-opt` | `no-new-privileges` | Cegah privilege escalation |

Kode dijalankan via `timeout {SANDBOX_TIMEOUT_SEC} python /work/script.py` — timeout ganda (Docker + OS timeout) untuk mencegah runaway.

**Output:**
```python
{
    "stdout": "...",   # Output standar (maks 4000 karakter)
    "stderr": "...",   # Error output (maks 2000 karakter)
    "exit_code": 0,    # Exit code proses
}
```

Jika asyncio timeout → `{"error": "Eksekusi melebihi timeout", "exit_code": -1}`.

---

## `tools/shell.py`

### `ShellRunTool`

Jalankan perintah shell read-only (grep, find, ls, cat, git log) **di dalam Docker sandbox**, bukan di host. **SELALU butuh approval**.

- `requires_approval = True` (selalu)
- Input: `{"command": "..."}`
- Output sukses: `{"stdout": "...", "stderr": "...", "exit_code": 0}`
- Output error: `{"error": "..."}` jika timeout, command kosong/terlalu panjang, atau **Docker tidak tersedia**

Batasan keamanan (via `DockerSandbox.run_shell`):
- `--network none` (tidak ada internet), `--read-only` filesystem
- Workspace di-mount **read-only** ke `/work` — tidak bisa memodifikasi file host
- Non-root (`--user nobody`), timeout 30 detik, memory/CPU limit
- **Tidak ada fallback ke host.** Jika Docker tidak ada → `SandboxUnavailable` → error (keamanan #1: tidak pernah eksekusi di host)

### `ListDirTool`

List isi direktori dalam workspace. Read-only — **tidak butuh approval**.

- `requires_approval = False`
- Input: `{"path": "..."}` — path direktori relatif ke workspace, opsional (default root workspace)
- Output sukses: `{"path": "...", "entries": [{"name": "...", "type": "dir|file"}, ...]}` — maks 200 entri
- Output error: `{"error": "..."}` jika di luar workspace, tidak ditemukan, bukan direktori, atau permission denied

---

## Tool Permission Matrix

| Tool | PM | QA | Dev | Butuh Approval |
|---|---|---|---|---|
| `file_read` | ✅ | ✅ | ✅ | Tidak |
| `file_write` | ✅ | ✅ | ✅ | **Ya** |
| `file_edit` | ❌ | ❌ | ✅ | **Ya** |
| `file_append` | ❌ | ❌ | ✅ | **Ya** |
| `apply_patch` | ❌ | ❌ | ✅ | **Ya** |
| `list_dir` | ✅ | ✅ | ✅ | Tidak |
| `glob` | ✅ | ✅ | ✅ | Tidak |
| `grep` | ✅ | ✅ | ✅ | Tidak |
| `pdf_read` | ✅ | ✅ | ✅ | Tidak |
| `shell_run` | ❌ | ✅ | ✅ | **Ya (selalu)** |
| `code_run` | ❌ | ✅ | ✅ | **Ya (selalu)** |
| `web_fetch` | ✅ | ❌ | ✅ | Tidak |
| `web_search` | ✅ | ❌ | ✅ | Tidak |
| `http_request` | ❌ | ❌ | ✅ | **Ya** |
| `db_query` | ❌ | ✅ | ✅ | **Ya** |
| `memory_search` | ✅ | ✅ | ✅ | Tidak |
| `json_query` | ✅ | ✅ | ✅ | Tidak |
| `ask_user` | ✅ | ✅ | ✅ | Tidak |

Permission dikontrol via `soul.toml[tools][allowed]` tiap role — bukan hardcoded di kode tool. Semua tool filesystem dibatasi ke `workspace_root` (lihat catatan di `TOOL_REGISTRY`).

---

## Cara Menambah Tool Baru

1. Buat class di file baru (atau di file yang relevan) yang extends `Tool`
2. Set `name`, `requires_approval`
3. Implementasi `execute()` dan `schema()`
4. Tambahkan instansi ke `TOOL_REGISTRY` di `tools/__init__.py`
5. Tambahkan nama tool ke `soul.toml[tools][allowed]` role yang butuh
6. Tulis test di `tests/test_tools.py`

> Tool yang mengubah state eksternal (filesystem, network write, system call) harus `requires_approval = True`.
