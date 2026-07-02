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
