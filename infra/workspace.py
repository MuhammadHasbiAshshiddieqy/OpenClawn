"""Workspace guard — batasi akses filesystem tool ke satu folder kerja.

Keamanan #1: tool file (read/write/edit/glob/grep/list_dir) TIDAK boleh menyentuh
file di luar `workspace_root`. Penyerang (atau model yang halusinasi) bisa mencoba
keluar lewat `../../etc/passwd` atau symlink yang menunjuk ke luar — keduanya
dipatahkan dengan me-`resolve()` path (collapse `..` + follow symlink) lalu
memastikan hasilnya masih di dalam root yang sudah di-resolve.

Modul ini sengaja kecil & tanpa dependency OpenCLAWN selain stdlib, agar mudah
diaudit dan dipakai ulang oleh semua tool.
"""

from pathlib import Path


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
