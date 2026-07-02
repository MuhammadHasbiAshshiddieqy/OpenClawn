"""Workspace guard — batasi akses filesystem tool ke satu folder kerja.

Keamanan #1: tool file (read/write/edit/glob/grep/list_dir) TIDAK boleh menyentuh
file di luar `workspace_root`. Penyerang (atau model yang halusinasi) bisa mencoba
keluar lewat `../../etc/passwd` atau symlink yang menunjuk ke luar — keduanya
dipatahkan dengan me-`resolve()` path (collapse `..` + follow symlink) lalu
memastikan hasilnya masih di dalam root yang sudah di-resolve.

Modul ini sengaja kecil & tanpa dependency OpenCLAWN selain stdlib, agar mudah
diaudit dan dipakai ulang oleh semua tool.

Working directory ADAPTIF per-sesi (§ user request, ala Claude Code/OpenClaw):
`CONFIG.workspace_root` tetap default global (env var, cocok localhost/single
folder), tapi tiap turn AgentLoop bisa menyetel `CURRENT_WORKSPACE_ROOT`
(`contextvars.ContextVar`) ke folder pilihan user untuk SESI itu. Semua tool
(`tools/file_ops.py` dll.) memanggil `CONFIG.workspace_root` langsung — mengubah
signature `Tool.execute()` di ~15 file demi ini terlalu invasif. ContextVar
menghindari itu: aman untuk request konkuren (tiap request py context sendiri,
tak saling menimpa seperti mutable global biasa) TANPA mengubah satu pun tool.
"""

import contextvars
from pathlib import Path

from infra.database import DatabaseManager

# Sengaja lebih longgar dari resolve_in_workspace: user (lewat UI field ATAU tool
# set_workdir) boleh memilih folder MANA PUN di mesinnya sendiri sebagai root baru
# — kebalikan dari resolve_in_workspace yang membatasi path ke SATU root tetap.

# None → pakai CONFIG.workspace_root (perilaku lama, tak ada perubahan). Diisi
# oleh AgentLoop.run() dari AgentConfig.workspace_override (kalau user mengisi
# field folder kerja di UI) sebelum tool loop berjalan.
CURRENT_WORKSPACE_ROOT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "CURRENT_WORKSPACE_ROOT", default=None
)


class WorkspaceViolation(Exception):
    """Di-raise saat path keluar dari workspace root."""


def resolve_in_workspace(candidate: str, workspace_root: str) -> Path:
    """Resolve `candidate` dan pastikan tetap di dalam `workspace_root`.

    Mengembalikan `Path` absolut yang sudah di-resolve bila aman. Me-raise
    `WorkspaceViolation` bila path keluar dari root (lewat `..`, absolute path,
    atau symlink). `candidate` boleh relatif (diukur dari root) atau absolut.
    """
    if not candidate or not candidate.strip():
        raise WorkspaceViolation("path kosong")

    root = Path(workspace_root).resolve()
    raw = Path(candidate)
    # Path relatif diukur dari workspace root, bukan cwd proses.
    base = raw if raw.is_absolute() else (root / raw)

    # resolve() meng-collapse '..' dan mengikuti symlink → escape terdeteksi di sini.
    # strict=False: file belum tentu ada (mis. file_write membuat file baru).
    resolved = base.resolve()

    if resolved != root and root not in resolved.parents:
        raise WorkspaceViolation(
            f"Path '{candidate}' di luar workspace. Akses dibatasi ke '{root}'."
        )
    return resolved


def effective_workspace_root(config_default: str) -> str:
    """`CURRENT_WORKSPACE_ROOT` bila diset (folder pilihan user untuk sesi ini),
    kalau tidak `config_default` (CONFIG.workspace_root, perilaku lama)."""
    override = CURRENT_WORKSPACE_ROOT.get()
    return override if override else config_default


def resolve_in_current_workspace(candidate: str, config_default: str) -> Path:
    """`resolve_in_workspace` tapi root-nya ikut `effective_workspace_root` —
    dipakai tool file (`tools/file_ops.py` dll.) menggantikan pemanggilan
    `resolve_in_workspace(path, CONFIG.workspace_root)` langsung, agar folder
    kerja per-sesi (§ working directory adaptif) otomatis terpakai tanpa
    mengubah signature `Tool.execute()`."""
    return resolve_in_workspace(candidate, effective_workspace_root(config_default))


def validate_workdir_candidate(raw: str) -> tuple[str | None, str | None]:
    """Validasi folder kerja pilihan user SEBELUM dipakai sebagai workspace root —
    fail-closed: path tak lolos TIDAK PERNAH diteruskan ke ContextVar/DB. Return
    `(resolved_path, None)` bila valid, `(None, error_message)` bila tidak.

    Dipakai DUA jalur (§ working directory adaptif + § user request "pindah
    direktori dinamis lewat chat"): field UI (`web/main.py` § `GET /workdir/check`,
    `/chat/stream`) dan tool `set_workdir` (`tools/workspace_tool.py`) — satu
    sumber kebenaran agar keduanya konsisten. Sengaja permisif soal LOKASI (lihat
    komentar di atas) — hanya mengecek path itu benar-benar ada & direktori, agar
    tool tak gagal aneh di tengah turn karena folder salah ketik/tak ada.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None  # kosong = tak ada override, bukan error
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"Folder '{raw}' tidak ditemukan atau tidak bisa diakses."
    if not p.is_dir():
        return None, f"'{raw}' bukan direktori."
    return str(p), None


class SessionWorkspaceStore:
    """Folder kerja aktif per-sesi, tersimpan di DB (§ user request: "pindah
    direktori secara dinamis" lewat chat, bukan cuma field UI sekali per-request).

    Terpisah dari `MemoryManager` (yang role-scoped) agar tool `set_workdir`
    (`tools/workspace_tool.py`) tak perlu import `memory/layers.py` — modul ini
    murni di atas `DatabaseManager`, sama pola `SettingsStore` (§ infra/settings.py).
    Satu baris per `session_id` (UPSERT): state "folder AKTIF sekarang", bukan riwayat.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get(self, session_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT workdir FROM session_workspace WHERE session_id=?", (session_id,)
        )
        return row["workdir"] if row else None

    async def set(self, session_id: str, workdir: str) -> None:
        await self.db.execute(
            """INSERT INTO session_workspace (session_id, workdir) VALUES (?, ?)
               ON CONFLICT(session_id) DO UPDATE SET workdir=excluded.workdir,
               updated_at=CURRENT_TIMESTAMP""",
            (session_id, workdir),
        )
