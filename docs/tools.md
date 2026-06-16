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
    "file_read":  FileReadTool(),
    "file_write": FileWriteTool(),
    "web_fetch":  WebFetchTool(),
    "ask_user":   AskUserTool(),
    "code_run":   CodeRunTool(),
}
```

`AgentLoop` mengakses registry ini untuk lookup dan schema generation.

---

## `tools/file_ops.py`

### `FileReadTool`

Baca isi file dari filesystem.

- `requires_approval = False`
- Input: `{"path": "..."}` — path file yang dibaca
- Output sukses: `{"content": "..."}` — isi file (maks 10.000 karakter)
- Output error: `{"error": "..."}` — pesan error jika file tidak ditemukan atau permission denied

### `FileWriteTool`

Tulis konten ke file. **Destruktif** → butuh approval.

- `requires_approval = True`
- Input: `{"path": "...", "content": "..."}`
- Output sukses: `{"ok": true, "path": "..."}`
- Output error: `{"error": "..."}` jika permission denied

---

## `tools/web.py`

### `WebFetchTool`

Fetch konten dari URL via HTTP GET.

- `requires_approval = False`
- Input: `{"url": "..."}`
- Output sukses: `{"status": 200, "content": "..."}` — konten teks (maks 5.000 karakter)
- Output error: `{"error": "..."}` jika HTTP error

Timeout 30 detik, ikut redirect otomatis.

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

## Tool Permission Matrix

| Tool | PM | QA | Dev | Butuh Approval |
|---|---|---|---|---|
| `file_read` | ✅ | ✅ | ✅ | Tidak |
| `file_write` | ✅ | ✅ | ✅ | **Ya** |
| `web_fetch` | ✅ | ❌ | ✅ | Tidak |
| `ask_user` | ✅ | ✅ | ❌ | Tidak |
| `code_run` | ❌ | ✅ | ✅ | **Ya (selalu)** |

Permission dikontrol via `soul.toml[tools][allowed]` tiap role — bukan hardcoded di kode tool.

---

## Cara Menambah Tool Baru

1. Buat class di file baru (atau di file yang relevan) yang extends `Tool`
2. Set `name`, `requires_approval`
3. Implementasi `execute()` dan `schema()`
4. Tambahkan instansi ke `TOOL_REGISTRY` di `tools/__init__.py`
5. Tambahkan nama tool ke `soul.toml[tools][allowed]` role yang butuh
6. Tulis test di `tests/test_tools.py`

> Tool yang mengubah state eksternal (filesystem, network write, system call) harus `requires_approval = True`.
