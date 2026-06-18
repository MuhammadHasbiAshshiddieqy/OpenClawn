import asyncio
import os
import tempfile
from pathlib import Path

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
            SANDBOX_IMAGE,
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
