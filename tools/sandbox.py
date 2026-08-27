import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from infra.sandbox_image import effective_sandbox_image

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
    def _base_docker_args(self, mount: str, tmpfs_size: str) -> list[str]:
        """Bangun argv `docker run` dengan SEMUA flag keamanan wajib.

        Satu sumber kebenaran untuk run_python & run_shell — sehingga test bisa
        memverifikasi argv NYATA (bukan rekonstruksi manual yang bisa divergen).
        `mount` = spec `-v src:/work:ro`; selalu read-only.
        """
        return [
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
            # § Prioritas 8.3: image proyek (dibangun build_project_image) bila
            # sesi ini punya satu aktif, kalau tidak SANDBOX_IMAGE dasar —
            # perilaku lama tak berubah untuk sesi yang tak pernah membangun.
            effective_sandbox_image(SANDBOX_IMAGE),
        ]

    async def run_python(self, code: str) -> dict:
        with tempfile.TemporaryDirectory() as workdir:
            script_path = os.path.join(workdir, "script.py")
            with open(script_path, "w") as f:
                f.write(code)

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
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC + 5
                )
                return {
                    "stdout": stdout.decode()[:4000],
                    "stderr": stderr.decode()[:2000],
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
        # workspace read-only — tidak bisa dimodifikasi; flag keamanan dari satu sumber.
        cmd = self._base_docker_args(f"{root}:/work:ro", "16m") + [
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
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC + 5
            )
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
