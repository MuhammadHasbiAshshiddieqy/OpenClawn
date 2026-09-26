import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from infra.config import CONFIG
from infra.logging import log
from infra.sandbox_image import effective_sandbox_image
from infra.sandbox_lifecycle import effective_persistent_container
from infra.workspace import is_sensitive_name

# Spesifikasi sandbox code_run (keamanan WAJIB):
# - Tidak ada akses network (--network none)
# - Mount read-only kecuali /tmp yang writable & ephemeral
# - Timeout keras
# - Resource limit (memory, CPU)
# - Non-root user
# - Tidak ada akses ke host filesystem atau credential

SANDBOX_IMAGE = "openclawn-sandbox:latest"
SANDBOX_TIMEOUT_SEC = 30
SANDBOX_MEM_LIMIT = "256m"
SANDBOX_CPU_LIMIT = "0.5"

# § Prioritas 8.3 (sandbox proyek besar/kompleks, keputusan owner: opsi (a) —
# image kustom per-proyek dengan dependency di-bake saat docker build, network
# hanya terbuka DI SITU, bukan saat docker run eksekusi kode sungguhan).
SANDBOX_BUILD_TIMEOUT_SEC = 300
PROJECT_IMAGE_PREFIX = "openclawn-sandbox-proj"

# § IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 3 (sandbox lifecycle,
# owner disetujui EKSPLISIT setelah trade-off keamanan dijelaskan — lihat
# infra/sandbox_lifecycle.py). Container/volume diberi nama DETERMINISTIK dari
# hash session_id (bukan random) — idempoten (panggilan create_persistent kedua
# untuk sesi yang sama menghasilkan nama yang sama, berguna untuk debugging via
# `docker ps`) dan menghindari perlu mem-parsing stdout `docker run` untuk id.
PERSISTENT_CONTAINER_PREFIX = "openclawn-persist"
PERSISTENT_TMPFS_SIZE = "64m"


# Audit 2026-09-25 (#3): batas pemindaian workspace saat mencari file credential
# untuk di-mask. Folder yang sama dilewati seperti tools/search.py (noise besar,
# bukan tempat .env). Batas atas mencegah scan workspace raksasa menunda tiap
# shell_run tanpa batas.
_MASK_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox"}
_MASK_MAX_DIRS = 5000
_MASK_MAX_ENTRIES = 100


def _sensitive_masks(root: str) -> list[str]:
    """Argumen docker yang menutupi file/folder credential di dalam workspace
    (`/dev/null` untuk file, tmpfs kosong untuk folder) sebelum di-mount ke
    /work. Tanpa ini `shell_run "cat .env"` (tanpa approval) membaca credential
    walau semua tool file sudah menolaknya.

    Residual risk jujur (§17): scan dibatasi `_MASK_MAX_DIRS` — workspace yang
    sangat besar bisa menyisakan file sensitif bersarang dalam yang tak
    ter-mask (di-log). Pertahanan utama tetap: jangan taruh credential di
    workspace; container tetap `--network none` jadi tak bisa kirim keluar
    sendiri."""
    masks: list[str] = []
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        visited += 1
        if visited > _MASK_MAX_DIRS or len(masks) >= _MASK_MAX_ENTRIES * 2:
            log.warning("sandbox_mask_scan_truncated", root=root, dirs=visited)
            break
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in dirnames:
            if d in _MASK_SKIP_DIRS:
                continue
            if is_sensitive_name(d):
                target = "/work/" + os.path.normpath(os.path.join(rel_dir, d))
                masks += ["--tmpfs", f"{target}:ro,size=1k"]
                continue  # jangan turun ke dalam folder yang sudah ditutup
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if is_sensitive_name(f):
                target = "/work/" + os.path.normpath(os.path.join(rel_dir, f))
                masks += ["-v", f"/dev/null:{target}:ro"]
    return masks


async def _kill_quietly(proc) -> None:
    """Matikan proses docker client yang melewati timeout — sebelumnya dibiarkan
    hidup (dan container-nya tetap jalan) setelah wait_for menyerah."""
    try:
        proc.kill()
        await proc.wait()
    except (ProcessLookupError, AttributeError, TypeError):
        pass
    except Exception as exc:  # noqa: BLE001 — pembersihan tak boleh menutupi hasil timeout
        log.warning("sandbox_kill_failed", error=str(exc))


class SandboxUnavailable(Exception):
    """Docker tidak tersedia — sandbox tidak bisa jalan. Fail-safe, jangan jalan di host."""


# Flag keamanan WAJIB pada setiap invocation docker run (CLAUDE.md §1.1).
# Dipakai oleh _base_docker_args() agar konstruksi argv tunggal & terverifikasi —
# bukan didefinisikan ulang per call site (sebelumnya: rawan flag terhapus diam-diam).
_REQUIRED_FLAGS: tuple[tuple[str, ...], ...] = (
    ("--network", "none"),  # isolasi network total
    ("--read-only",),  # root filesystem read-only
    ("--user", "nobody"),  # non-root
    ("--security-opt", "no-new-privileges"),  # cegah escalation via setuid
)


class DockerSandbox:
    def _base_docker_args(
        self, mount: str, tmpfs_size: str, extra: list[str] | None = None
    ) -> list[str]:
        """Bangun argv `docker run` dengan SEMUA flag keamanan wajib.

        Satu sumber kebenaran untuk run_python & run_shell — sehingga test bisa
        memverifikasi argv NYATA (bukan rekonstruksi manual yang bisa divergen).
        `mount` = spec `-v src:/work:ro`; selalu read-only. `extra` = mount
        tambahan (mask credential, lihat `_sensitive_masks`) sebelum image.
        """
        args = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            SANDBOX_MEM_LIMIT,
            "--cpus",
            SANDBOX_CPU_LIMIT,
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,size={tmpfs_size}",
            "-v",
            mount,
            "--workdir",
            "/work",
            "--user",
            "nobody",
            "--security-opt",
            "no-new-privileges",
        ]
        if extra:
            args += extra
        # § IMPROVEMENT-Sandbox-Isolation-Parallelization.md Fase 5 (runtime
        # isolasi pluggable): HANYA diteruskan bila operator eksplisit memilih
        # runtime non-default (mis. "runsc" untuk gVisor) — default "runc" tak
        # pernah menyentuh argv sama sekali, perilaku lama utuh tak berubah.
        if CONFIG.sandbox_runtime != "runc":
            args += ["--runtime", CONFIG.sandbox_runtime]
        args.append(
            # § Prioritas 8.3: image proyek (dibangun build_project_image) bila
            # sesi ini punya satu aktif, kalau tidak SANDBOX_IMAGE dasar —
            # perilaku lama tak berubah untuk sesi yang tak pernah membangun.
            effective_sandbox_image(SANDBOX_IMAGE)
        )
        return args

    async def run_python(self, code: str) -> dict:
        # § Fase 3 (sandbox lifecycle): sesi yang sudah punya sandbox persisten
        # aktif (tool sandbox_persist_enable) exec ke container yang SAMA alih-
        # alih docker run --rm sekali-pakai — state (file, package terinstall)
        # bertahan lintas panggilan code_run dalam sesi itu. None (default,
        # mayoritas sesi) → jalur ephemeral lama tak berubah sama sekali.
        container_id = effective_persistent_container()
        if container_id is not None:
            return await self.exec_persistent(container_id, code)

        with tempfile.TemporaryDirectory(dir=CONFIG.sandbox_tmp_dir) as workdir:
            script_path = os.path.join(workdir, "script.py")
            with open(script_path, "w") as f:
                f.write(code)
            # Audit 2026-09-26: TemporaryDirectory dibuat 0700 — container berjalan
            # sebagai `nobody` sehingga di host Linux (dan DinD) SETIAP code_run
            # gagal "Permission denied" membaca /work/script.py. Tak terlihat di
            # macOS karena Docker Desktop melonggarkan izin berkas. Mount tetap :ro.
            os.chmod(workdir, 0o755)
            os.chmod(script_path, 0o644)

            cmd = self._base_docker_args(f"{workdir}:/work:ro", "64m") + [
                "timeout",
                str(SANDBOX_TIMEOUT_SEC),
                "python",
                "/work/script.py",
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC + 5
                    )
                except asyncio.TimeoutError:
                    await _kill_quietly(proc)
                    raise
                # errors="replace": output non-UTF8 (mis. sys.stdout.buffer.write(b"\xff"))
                # sebelumnya me-raise UnicodeDecodeError di sini (audit 2026-09-25).
                return {
                    "stdout": stdout.decode(errors="replace")[:4000],
                    "stderr": stderr.decode(errors="replace")[:2000],
                    "exit_code": proc.returncode,
                }
            except asyncio.TimeoutError:
                return {"error": "Eksekusi melebihi timeout", "exit_code": -1}
            except FileNotFoundError as e:
                # Docker tidak terpasang → fail-safe, JANGAN fallback ke host (keamanan #1).
                raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e

    async def run_shell(self, command: str, workspace_root: str) -> dict:
        """Jalankan perintah shell read-only di dalam container terisolasi.

        Workspace di-mount READ-ONLY ke /work (--read-only filesystem + --network none),
        sehingga perintah seperti grep/find/ls/git aman: tidak bisa menulis ke host,
        tidak bisa keluar ke network, tidak bisa baca file di luar workspace yang dimount.
        """
        root = str(Path(workspace_root).resolve())
        # Audit 2026-09-25 (#3): tutupi credential di workspace sebelum mount.
        # Scan filesystem di thread agar tak memblokir event loop.
        masks = await asyncio.to_thread(_sensitive_masks, root)
        # workspace read-only — tidak bisa dimodifikasi; flag keamanan dari satu sumber.
        cmd = self._base_docker_args(f"{root}:/work:ro", "16m", masks) + [
            "timeout",
            str(SANDBOX_TIMEOUT_SEC),
            "sh",
            "-c",
            command,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC + 5
                )
            except asyncio.TimeoutError:
                await _kill_quietly(proc)
                raise
            return {
                "stdout": stdout.decode(errors="replace")[:4000],
                "stderr": stderr.decode(errors="replace")[:2000],
                "exit_code": proc.returncode,
            }
        except asyncio.TimeoutError:
            return {"error": "Perintah melebihi timeout", "exit_code": -1}
        except FileNotFoundError as e:
            raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e

    async def build_project_image(self, requirements_content: str) -> dict:
        """Bangun image sandbox KHUSUS satu proyek dengan dependency Python
        di-*bake* saat `docker build` — network HANYA terbuka DI SINI, bukan
        saat `docker run` eksekusi kode sungguhan (§ Prioritas 8.3, keputusan
        owner: opsi (a) dari 3 kandidat). Image dibangun FROM `SANDBOX_IMAGE`
        dasar (mewarisi semua properti keamanan lain — non-root, tanpa tooling
        ekstra), HANYA menambah `pip install -r requirements.txt`.

        Residual risk yang JUJUR didokumentasikan (§1/§17 — jangan beri rasa
        aman palsu): `pip install` bisa menjalankan kode arbitrer dari
        `setup.py`/build backend paket pihak ketiga SELAMA build — risiko
        inheren memakai pip apa pun sumbernya, bukan sesuatu yang bisa
        disandbox lebih jauh tanpa membangun ulang PyPI. Mitigasi ada di
        lapisan caller (`tools/sandbox_image.py::_validate_requirements`):
        baris yang mengandung opsi pip (`-e`/`--index-url`/`-r`/dst, apa pun
        yang diawali `-`) ditolak SEBELUM sampai sini — mencegah pengalihan ke
        index pihak ketiga tak tepercaya atau instalasi VCS/lokal arbitrer.

        Cache: image dengan tag yang SAMA (hash konten `requirements.txt`)
        di-skip rebuild — `docker image inspect` dulu, TANPA network sama
        sekali bila sudah pernah dibangun.

        Return `{"ok", "image", "cached", "error", "log_tail"}`.
        `SandboxUnavailable` di-raise HANYA bila Docker sendiri tak terpasang
        (pola sama run_python/run_shell) — kegagalan build (mis. paket tak ada
        di PyPI) dikembalikan sebagai dict, bukan exception, agar model bisa
        membaca pesan error & mencoba lagi.
        """
        content_hash = hashlib.sha256(requirements_content.encode()).hexdigest()[:12]
        tag = f"{PROJECT_IMAGE_PREFIX}:{content_hash}"

        try:
            inspect = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "inspect",
                tag,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await inspect.wait()
        except FileNotFoundError as e:
            raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e
        if inspect.returncode == 0:
            return {"ok": True, "image": tag, "cached": True, "error": None, "log_tail": ""}

        # Build context TERISOLASI (hanya requirements.txt + Dockerfile yang
        # dihasilkan) — BUKAN seluruh workspace, agar tak ada file/secret proyek
        # lain yang ikut terkirim ke proses build.
        with tempfile.TemporaryDirectory() as build_dir:
            (Path(build_dir) / "requirements.txt").write_text(requirements_content)
            (Path(build_dir) / "Dockerfile").write_text(
                f"FROM {SANDBOX_IMAGE}\n"
                "USER root\n"
                "COPY requirements.txt /tmp/requirements.txt\n"
                "RUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
                "USER nobody\n"
            )
            # SENGAJA TANPA --network none — satu-satunya invocation Docker di
            # seluruh modul ini yang network-nya terbuka, dan hanya untuk
            # `docker build`, tidak pernah untuk `docker run` eksekusi kode.
            cmd = ["docker", "build", "-t", tag, build_dir]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=SANDBOX_BUILD_TIMEOUT_SEC
                )
            except asyncio.TimeoutError:
                # Tak ada wrapper `timeout` command portabel di level ini (build
                # jalan di HOST, bukan di dalam container seperti run_python/
                # run_shell) — bunuh proses eksplisit, jangan biarkan build
                # menggantung tak terbatas di background.
                proc.kill()
                await proc.wait()
                return {
                    "ok": False,
                    "image": None,
                    "cached": False,
                    "error": f"Build melebihi timeout {SANDBOX_BUILD_TIMEOUT_SEC}s",
                    "log_tail": "",
                }
            if proc.returncode != 0:
                return {
                    "ok": False,
                    "image": None,
                    "cached": False,
                    "error": "docker build gagal (lihat log_tail)",
                    "log_tail": stderr.decode(errors="replace")[-2000:],
                }

        return {"ok": True, "image": tag, "cached": False, "error": None, "log_tail": ""}

    async def create_persistent(self, session_id: str) -> dict:
        """Buat container sandbox PERSISTEN untuk satu sesi (§ Fase 3, sandbox
        lifecycle) — `docker run -d` (BUKAN `--rm`), tetap mewarisi SEMUA flag
        keamanan wajib (`--network none`, `--read-only`, non-root,
        `no-new-privileges`, `--runtime` bila non-default) dari sumber yang
        SAMA dengan sandbox ephemeral. HANYA `/work` yang berbeda: named Docker
        volume (writable, bertahan lintas `docker exec`) menggantikan mount
        temp-dir read-only sekali-pakai — root filesystem TETAP `--read-only`.

        `sleep infinity` sebagai command container: idiom standar agar
        container tetap hidup menunggu `docker exec` berikutnya (tak ada
        proses lain yang perlu dijalankan saat container baru dibuat).

        Nama container/volume DETERMINISTIK dari hash `session_id` (bukan
        random) — panggilan kedua untuk sesi yang sama menghasilkan nama
        yang sama (idempoten, memudahkan debug via `docker ps`), dan
        menghindari perlu mem-parsing stdout `docker run` untuk container id.

        Return `{"ok": True, "container_id", "volume_name"}` atau
        `{"ok": False, "error"}`. `SandboxUnavailable` HANYA bila Docker
        sendiri tak terpasang (pola sama method lain di kelas ini).
        """
        suffix = hashlib.sha256(session_id.encode()).hexdigest()[:12]
        container_id = f"{PERSISTENT_CONTAINER_PREFIX}-{suffix}"
        volume_name = f"{PERSISTENT_CONTAINER_PREFIX}-vol-{suffix}"

        args = [
            "docker",
            "run",
            "-d",
            "--name",
            container_id,
            "--network",
            "none",
            "--memory",
            SANDBOX_MEM_LIMIT,
            "--cpus",
            SANDBOX_CPU_LIMIT,
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,size={PERSISTENT_TMPFS_SIZE}",
            "-v",
            f"{volume_name}:/work",
            "--workdir",
            "/work",
            "--user",
            "nobody",
            "--security-opt",
            "no-new-privileges",
        ]
        if CONFIG.sandbox_runtime != "runc":
            args += ["--runtime", CONFIG.sandbox_runtime]
        args += [effective_sandbox_image(SANDBOX_IMAGE), "sleep", "infinity"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError as e:
            raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e

        if proc.returncode != 0:
            return {"ok": False, "error": stderr.decode(errors="replace")[:1000]}
        return {"ok": True, "container_id": container_id, "volume_name": volume_name}

    async def exec_persistent(self, container_id: str, code: str) -> dict:
        """Jalankan `code` di dalam container PERSISTEN yang sudah hidup
        (`docker exec`, bukan `docker run` baru) — hasil tulis-file di
        panggilan ini terlihat di panggilan `code_run` BERIKUTNYA dalam sesi
        yang sama, selama container belum di-destroy `core/sandbox_reaper.py`.

        Dua langkah: (1) tulis `code` ke `/work/script.py` di dalam container
        via `docker exec -i ... sh -c 'cat > ...'` (stdin pipe — tak ada
        argumen shell yang berisi kode arbitrer, jadi tak ada risiko shell
        injection dari isi `code`); (2) jalankan filenya. Timeout keras sama
        seperti jalur ephemeral (`SANDBOX_TIMEOUT_SEC`).
        """
        write_cmd = ["docker", "exec", "-i", container_id, "sh", "-c", "cat > /work/script.py"]
        try:
            write_proc = await asyncio.create_subprocess_exec(
                *write_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, write_err = await write_proc.communicate(input=code.encode())
        except FileNotFoundError as e:
            raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e
        if write_proc.returncode != 0:
            return {
                "error": f"Gagal menulis kode ke sandbox persisten: "
                f"{write_err.decode(errors='replace')[:500]}",
                "exit_code": -1,
            }

        run_cmd = [
            "docker",
            "exec",
            container_id,
            "timeout",
            str(SANDBOX_TIMEOUT_SEC),
            "python",
            "/work/script.py",
        ]
        try:
            run_proc = await asyncio.create_subprocess_exec(
                *run_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    run_proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC + 5
                )
            except asyncio.TimeoutError:
                await _kill_quietly(run_proc)
                raise
            return {
                "stdout": stdout.decode(errors="replace")[:4000],
                "stderr": stderr.decode(errors="replace")[:2000],
                "exit_code": run_proc.returncode,
            }
        except asyncio.TimeoutError:
            return {"error": "Eksekusi melebihi timeout", "exit_code": -1}
        except FileNotFoundError as e:
            raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e

    async def pause_persistent(self, container_id: str) -> dict:
        """`docker pause` — bekukan SEMUA proses di container (hemat CPU host)
        tanpa menghapusnya; state `/work` tetap ada, `resume_persistent`
        melanjutkan persis dari titik berhenti. Dipanggil `core/sandbox_reaper.py`
        setelah `AppConfig.sandbox_persist_idle_ttl_sec` tanpa pemakaian."""
        return await self._run_lifecycle_command(["docker", "pause", container_id])

    async def resume_persistent(self, container_id: str) -> dict:
        """`docker unpause` — kebalikan `pause_persistent`. Dipanggil otomatis
        (`AgentLoop.run()`/`core/agent_loop.py`) begitu sesi yang container-nya
        sedang paused memanggil `code_run` lagi — transparan bagi model."""
        return await self._run_lifecycle_command(["docker", "unpause", container_id])

    async def _run_lifecycle_command(self, cmd: list[str]) -> dict:
        """Helper bersama `pause_persistent`/`resume_persistent` — perintah
        Docker sederhana tanpa output yang perlu diparsing, hanya sukses/gagal."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError as e:
            raise SandboxUnavailable("Docker tidak tersedia di environment ini") from e
        if proc.returncode != 0:
            return {"ok": False, "error": stderr.decode(errors="replace")[:500]}
        return {"ok": True, "error": None}

    async def destroy_persistent(self, container_id: str, volume_name: str) -> None:
        """Hapus container + volume PERMANEN (`docker rm -f` lalu `docker
        volume rm`) — dipanggil `core/sandbox_reaper.py` setelah
        `AppConfig.sandbox_persist_destroy_ttl_sec` tanpa pemakaian.

        Fail-soft SEPENUHNYA (tak pernah raise, hanya log) — penghancuran
        pembersihan tak boleh diblokir oleh container yang sudah setengah
        rusak; caller (reaper) tetap menghapus baris DB-nya terlepas dari
        hasil ini (baris DB adalah "niat sudah di-destroy", bukan cermin
        status Docker real-time)."""
        for cmd in (["docker", "rm", "-f", container_id], ["docker", "volume", "rm", volume_name]):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    log.warning(
                        "sandbox_persistent_destroy_step_failed",
                        cmd=" ".join(cmd),
                        error=stderr.decode(errors="replace")[:500],
                    )
            except FileNotFoundError:
                log.warning("sandbox_persistent_destroy_docker_missing", cmd=" ".join(cmd))
                return
            except Exception as exc:  # noqa: BLE001 — pembersihan tak boleh raise ke caller
                log.warning("sandbox_persistent_destroy_failed", cmd=" ".join(cmd), error=str(exc))
