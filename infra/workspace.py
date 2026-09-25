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
from collections.abc import Sequence
from pathlib import Path

import infra.config as _config_mod
from infra.config import AppConfig
from infra.database import DatabaseManager

# Audit 2026-09-25 (#1, kritis): folder kerja pilihan user (UI field ATAU tool
# set_workdir) SEBELUMNYA boleh folder MANA PUN — termasuk `/`. Di deployment
# multi-user itu berarti user login mana pun (bahkan viewer) bisa membaca
# `/proc/self/environ` (semua API key + OPENCLAWN_ENCRYPTION_KEY) lewat
# file_read tanpa approval, atau mengunduh DB seluruh tenant lewat
# /workspace/download. Sekarang dibatasi ke allowlist root (lihat
# `default_workdir_roots`) — tetap lebih longgar dari resolve_in_workspace
# (banyak root, bukan satu), tapi tak lagi tanpa batas.

# None → pakai CONFIG.workspace_root (perilaku lama, tak ada perubahan). Diisi
# oleh AgentLoop.run() dari AgentConfig.workspace_override (kalau user mengisi
# field folder kerja di UI) sebelum tool loop berjalan.
CURRENT_WORKSPACE_ROOT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "CURRENT_WORKSPACE_ROOT", default=None
)

# Allowlist root folder kerja untuk turn yang sedang berjalan. None → default
# config (`default_workdir_roots(CONFIG)`). Diisi AgentLoop.run() dari
# AgentConfig.workdir_roots — web/main.py menghitungnya per-request dari RBAC
# (non-admin saat auth aktif → tuple kosong = tak boleh pindah folder sama
# sekali). Subtask Task Graph mewarisi nilai ini otomatis (create_task menyalin
# context), jadi member tak bisa melebarkan akses lewat subtask.
CURRENT_WORKDIR_ROOTS: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "CURRENT_WORKDIR_ROOTS", default=None
)

# Audit 2026-09-25 (#3): nama file/folder yang berisi credential — ditolak oleh
# SEMUA tool file (read/write/grep/glob/download) dan di-mask di sandbox
# shell_run. Sebelumnya workspace default `.` (root repo) membuat `.env` bisa
# dibaca file_read TANPA approval lalu dikirim keluar lewat web_fetch (juga
# tanpa approval) — satu prompt injection cukup untuk mencuri semua API key.
_SENSITIVE_NAMES = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".docker",
        ".kube",
        ".netrc",
        ".pgpass",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
    }
)
_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})


class WorkspaceViolation(Exception):
    """Di-raise saat path keluar dari workspace root."""


def is_sensitive_name(name: str) -> bool:
    """True bila satu komponen path adalah file/folder credential yang dikenal."""
    n = name.lower()
    if n in _SENSITIVE_NAMES:
        return True
    if n == ".env" or (n.startswith(".env.") and n not in _ENV_TEMPLATE_NAMES):
        return True
    return False


def _app_data_files(config: AppConfig) -> set[Path]:
    """File data internal aplikasi (DB SQLite + WAL/SHM + anchor audit) — isinya
    seluruh tenant, tak boleh dibaca lewat tool agent walau ada di workspace."""
    files: set[Path] = set()
    if config.db_path and config.db_path != ":memory:":
        db = Path(config.db_path).resolve()
        files |= {db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")}
    if config.audit_anchor_path:
        files.add(Path(config.audit_anchor_path).resolve())
    return files


def _current_config() -> AppConfig:
    # Dibaca saat dipanggil (bukan diikat saat import) — config bisa di-reload
    # (test web, atau reload proses) tanpa modul ini memegang objek basi.
    return _config_mod.CONFIG


def is_sensitive_path(path: Path, config: AppConfig | None = None) -> bool:
    """True bila `path` (absolut, sudah di-resolve) menunjuk ke credential atau
    file data internal. Memeriksa SEMUA komponen path, bukan cuma nama akhir —
    `~/.ssh/config` dan `proj/.env.local` sama-sama ditolak."""
    if any(is_sensitive_name(part) for part in path.parts):
        return True
    return path in _app_data_files(config or _current_config())


def default_workdir_roots(config: AppConfig | None = None) -> tuple[str, ...]:
    """Allowlist root folder kerja bila tak ada yang lebih spesifik.

    - `config.workdir_allowed_roots` diisi operator → itu yang dipakai.
    - Kosong + auth NONAKTIF (localhost single-user) → home user + workspace
      default — tetap mendukung pola "kerja di ~/project-y", tapi `/`, `/etc`,
      `/proc` dan sejenisnya tertutup.
    - Kosong + auth AKTIF (multi-user) → tuple kosong: tak ada yang boleh
      pindah folder sama sekali sampai operator memilih root yang aman.
    """
    config = config or _current_config()
    if config.workdir_allowed_roots:
        return tuple(config.workdir_allowed_roots)
    if config.auth_active:
        return ()
    return (str(Path.home()), config.workspace_root)


def effective_workdir_roots() -> tuple[str, ...]:
    """Allowlist yang berlaku untuk turn ini (ContextVar bila diset, else default)."""
    roots = CURRENT_WORKDIR_ROOTS.get()
    return roots if roots is not None else default_workdir_roots()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


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

    if not _within(resolved, root):
        raise WorkspaceViolation(
            f"Path '{candidate}' di luar workspace. Akses dibatasi ke '{root}'."
        )
    # Audit 2026-09-25 (#3): credential & DB internal tak pernah boleh disentuh
    # tool agent, walau secara fisik ada di dalam workspace.
    if is_sensitive_path(resolved):
        raise WorkspaceViolation(
            f"Akses ke '{candidate}' ditolak: file credential/data internal dilindungi."
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


def validate_workdir_candidate(
    raw: str, allowed_roots: Sequence[str] | None = None
) -> tuple[str | None, str | None]:
    """Validasi folder kerja pilihan user SEBELUM dipakai sebagai workspace root —
    fail-closed: path tak lolos TIDAK PERNAH diteruskan ke ContextVar/DB. Return
    `(resolved_path, None)` bila valid, `(None, error_message)` bila tidak.

    Dipakai DUA jalur (§ working directory adaptif + § user request "pindah
    direktori dinamis lewat chat"): field UI (`web/main.py` § `GET /workdir/check`,
    `/chat/stream`) dan tool `set_workdir` (`tools/workspace_tool.py`) — satu
    sumber kebenaran agar keduanya konsisten.

    `allowed_roots` None → `effective_workdir_roots()` (ContextVar turn ini atau
    default config). Folder harus berada DI DALAM salah satu root (audit
    2026-09-25 #1) dan bukan folder credential (`.ssh`, `.aws`, ...).
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

    roots = tuple(allowed_roots) if allowed_roots is not None else effective_workdir_roots()
    if not roots:
        return None, (
            "Mengganti folder kerja tidak diizinkan di deployment ini "
            "(minta admin mengisi OPENCLAWN_WORKDIR_ROOTS)."
        )
    resolved_roots = []
    for r in roots:
        try:
            resolved_roots.append(Path(r).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    if not any(_within(p, root) for root in resolved_roots):
        return None, (
            f"Folder '{raw}' di luar area yang diizinkan "
            f"({', '.join(str(r) for r in resolved_roots)})."
        )
    if is_sensitive_path(p):
        return (
            None,
            f"Folder '{raw}' berisi credential dan tidak boleh dipakai sebagai folder kerja.",
        )
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
