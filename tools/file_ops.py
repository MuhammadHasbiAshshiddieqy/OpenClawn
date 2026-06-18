import aiofiles

from infra.config import CONFIG
from infra.workspace import WorkspaceViolation, resolve_in_workspace
from tools.base import Tool

MAX_READ = CONFIG.tool_max_output
# read_many: batasi jumlah file per panggilan & porsi per file agar tidak membanjiri
# context (token-first §1.4). Total tetap dijepit jaring pengaman di _execute_tool.
MAX_FILES_PER_BATCH = 10
PER_FILE_BUDGET = 4_000


class FileReadTool(Tool):
    name = "file_read"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = input_data.get("path", "")
        if not path:
            return {"error": "path wajib diisi"}
        try:
            # Keamanan #1: tolak path di luar workspace (anti ../ & symlink).
            safe = resolve_in_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}
        try:
            async with aiofiles.open(safe) as f:
                content = await f.read()
            truncated = len(content) > MAX_READ
            return {"content": content[:MAX_READ], "truncated": truncated}
        except FileNotFoundError:
            return {"error": f"File tidak ditemukan: {path}"}
        except IsADirectoryError:
            return {"error": f"'{path}' adalah direktori, bukan file. Gunakan list_dir."}
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}
        except UnicodeDecodeError:
            return {"error": f"File bukan teks (binary?): {path}"}

    def schema(self) -> dict:
        return {
            "name": "file_read",
            "description": (
                "Baca isi file teks. Path relatif terhadap workspace; "
                "akses di luar workspace ditolak. Gunakan sebelum mengedit file."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }


class ReadManyTool(Tool):
    """Baca beberapa file dalam SATU panggilan → hemat tool hop & token vs N file_read.

    Tiap path divalidasi workspace-safe terpisah; satu file gagal tidak menggagalkan
    yang lain (kegagalan per-file dilaporkan, bukan exception). Read-only — tanpa approval.
    """

    name = "read_many"
    requires_approval = False

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        paths = input_data.get("paths")
        if not isinstance(paths, list) or not paths:
            return {"error": "paths wajib berupa list path (minimal satu)"}
        capped = paths[:MAX_FILES_PER_BATCH]
        files: list[dict] = []
        for path in capped:
            entry: dict = {"path": path}
            try:
                safe = resolve_in_workspace(str(path), CONFIG.workspace_root)
            except WorkspaceViolation as e:
                entry["error"] = str(e)
                files.append(entry)
                continue
            try:
                async with aiofiles.open(safe) as f:
                    content = await f.read()
                entry["content"] = content[:PER_FILE_BUDGET]
                entry["truncated"] = len(content) > PER_FILE_BUDGET
            except FileNotFoundError:
                entry["error"] = "tidak ditemukan"
            except (IsADirectoryError, PermissionError, UnicodeDecodeError) as e:
                entry["error"] = type(e).__name__
            files.append(entry)
        return {
            "files": files,
            "count": len(files),
            "skipped": max(0, len(paths) - len(capped)),
        }

    def schema(self) -> dict:
        return {
            "name": "read_many",
            "description": (
                f"Baca beberapa file teks sekaligus (maks {MAX_FILES_PER_BATCH} per panggilan). "
                "Lebih efisien dari memanggil file_read berulang. Path relatif ke workspace; "
                "file di luar workspace ditolak. Tiap file dipotong agar context tetap ringkas."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Daftar path file relatif ke workspace.",
                    }
                },
                "required": ["paths"],
            },
        }


class FileWriteTool(Tool):
    name = "file_write"
    requires_approval = True  # tool destruktif — memodifikasi filesystem user

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = input_data.get("path", "")
        content = input_data.get("content", "")
        if not path:
            return {"error": "path wajib diisi"}
        try:
            safe = resolve_in_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}
        try:
            # Buat folder induk bila belum ada (masih dalam workspace — sudah divalidasi).
            safe.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(safe, "w") as f:
                await f.write(content)
            return {"ok": True, "path": str(safe), "bytes": len(content)}
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}
        except IsADirectoryError:
            return {"error": f"'{path}' adalah direktori."}

    def schema(self) -> dict:
        return {
            "name": "file_write",
            "description": (
                "Tulis (atau timpa) seluruh isi file. Untuk perubahan kecil pada file "
                "yang sudah ada, lebih baik gunakan file_edit. Path dibatasi ke workspace. "
                "SELALU butuh persetujuan user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }


class FileEditTool(Tool):
    """Edit parsial: ganti `old_string` jadi `new_string` di file yang sudah ada.

    Lebih aman & hemat token dari file_write untuk perubahan kecil. `old_string`
    harus cocok PERSIS dan unik (kecuali replace_all) agar tidak salah edit.
    """

    name = "file_edit"
    requires_approval = True  # destruktif — memodifikasi file

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = input_data.get("path", "")
        old = input_data.get("old_string", "")
        new = input_data.get("new_string", "")
        replace_all = bool(input_data.get("replace_all", False))
        if not path:
            return {"error": "path wajib diisi"}
        if not old:
            return {"error": "old_string wajib diisi (teks yang akan diganti)"}
        if old == new:
            return {"error": "old_string dan new_string identik — tidak ada perubahan"}
        try:
            safe = resolve_in_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}

        try:
            async with aiofiles.open(safe) as f:
                content = await f.read()
        except FileNotFoundError:
            return {"error": f"File tidak ditemukan: {path}"}
        except (PermissionError, UnicodeDecodeError) as e:
            return {"error": f"Gagal membaca {path}: {e}"}

        count = content.count(old)
        if count == 0:
            return {"error": "old_string tidak ditemukan di file (harus cocok persis)"}
        if count > 1 and not replace_all:
            return {
                "error": (
                    f"old_string muncul {count}x — tidak unik. Perpanjang konteksnya agar "
                    "unik, atau set replace_all=true untuk mengganti semua."
                )
            }

        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        try:
            async with aiofiles.open(safe, "w") as f:
                await f.write(updated)
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}
        return {"ok": True, "path": str(safe), "replacements": count if replace_all else 1}

    def schema(self) -> dict:
        return {
            "name": "file_edit",
            "description": (
                "Ganti potongan teks di file yang sudah ada (edit parsial). "
                "old_string harus cocok PERSIS dengan isi file dan unik (kecuali replace_all). "
                "Baca file dulu dengan file_read agar old_string tepat. Path dibatasi ke workspace. "
                "SELALU butuh persetujuan user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string", "description": "Teks lama (cocok persis)."},
                    "new_string": {"type": "string", "description": "Teks pengganti."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Ganti semua kemunculan, bukan hanya yang pertama.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        }


class FileAppendTool(Tool):
    """Tambah konten ke akhir file (buat jika belum ada). Destruktif → approval."""

    name = "file_append"
    requires_approval = True

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = input_data.get("path", "")
        content = input_data.get("content", "")
        if not path:
            return {"error": "path wajib diisi"}
        if not content:
            return {"error": "content wajib diisi"}
        try:
            safe = resolve_in_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}
        try:
            safe.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(safe, "a") as f:
                await f.write(content)
            return {"ok": True, "path": str(safe), "appended": len(content)}
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}
        except IsADirectoryError:
            return {"error": f"'{path}' adalah direktori."}

    def schema(self) -> dict:
        return {
            "name": "file_append",
            "description": (
                "Tambahkan konten ke AKHIR file tanpa menimpa isi lama (buat file bila belum ada). "
                "Cocok untuk log/catatan. Path dibatasi ke workspace. SELALU butuh persetujuan user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }


class ApplyPatchTool(Tool):
    """Multi-edit atomik: beberapa (old→new) dalam satu file sekaligus.

    Atomik: bila SALAH SATU edit tidak cocok/tidak unik, TIDAK ada yang ditulis.
    Mencegah file setengah ter-edit. Destruktif → approval.
    """

    name = "apply_patch"
    requires_approval = True

    async def execute(self, input_data: dict, vault, db=None) -> dict:
        path = input_data.get("path", "")
        edits = input_data.get("edits")
        if not path:
            return {"error": "path wajib diisi"}
        if not isinstance(edits, list) or not edits:
            return {"error": "edits wajib berupa list non-kosong [{old_string,new_string}]"}
        try:
            safe = resolve_in_workspace(path, CONFIG.workspace_root)
        except WorkspaceViolation as e:
            return {"error": str(e)}
        try:
            async with aiofiles.open(safe) as f:
                content = await f.read()
        except FileNotFoundError:
            return {"error": f"File tidak ditemukan: {path}"}
        except (PermissionError, UnicodeDecodeError) as e:
            return {"error": f"Gagal membaca {path}: {e}"}

        # Validasi SEMUA edit dulu (atomik) sebelum menulis apa pun.
        working = content
        for i, ed in enumerate(edits):
            old = ed.get("old_string", "")
            new = ed.get("new_string", "")
            if not old:
                return {"error": f"edit #{i + 1}: old_string kosong"}
            cnt = working.count(old)
            if cnt == 0:
                return {
                    "error": f"edit #{i + 1}: old_string tidak ditemukan (mungkin sudah diubah edit sebelumnya)"
                }
            if cnt > 1:
                return {
                    "error": f"edit #{i + 1}: old_string muncul {cnt}x — tidak unik, perpanjang konteks"
                }
            working = working.replace(old, new, 1)

        try:
            async with aiofiles.open(safe, "w") as f:
                await f.write(working)
        except PermissionError:
            return {"error": f"Akses ditolak: {path}"}
        return {"ok": True, "path": str(safe), "edits_applied": len(edits)}

    def schema(self) -> dict:
        return {
            "name": "apply_patch",
            "description": (
                "Terapkan beberapa edit (old_string→new_string) ke SATU file secara atomik. "
                "Setiap old_string harus cocok persis & unik. Bila ada satu yang gagal, "
                "tidak ada perubahan ditulis (file tidak setengah ter-edit). "
                "Pakai untuk beberapa perubahan sekaligus di file yang sama. Path dibatasi ke "
                "workspace. SELALU butuh persetujuan user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "description": "Daftar edit, tiap item {old_string, new_string}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        }
