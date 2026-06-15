import asyncio
import os
import tempfile

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


class DockerSandbox:
    async def run_python(self, code: str) -> dict:
        with tempfile.TemporaryDirectory() as workdir:
            script_path = os.path.join(workdir, "script.py")
            with open(script_path, "w") as f:
                f.write(code)

            cmd = [
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
                "/tmp:rw,size=64m",
                "-v",
                f"{workdir}:/work:ro",
                "--workdir",
                "/work",
                "--user",
                "nobody",
                "--security-opt",
                "no-new-privileges",
                SANDBOX_IMAGE,
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
