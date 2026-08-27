"""Jalankan eval harness lewat agent SUNGGUHAN (TODO.md § Prioritas 8.2).

BUKAN bagian suite pytest (CLAUDE.md §5: LLM harus di-mock di pytest) —
skrip ini justru sebaliknya, sengaja memanggil Ollama nyata untuk mendeteksi
regresi kualitas jawaban yang tak bisa terlihat dari test dengan LLM di-mock.
Pola sama `scripts/seed_routing.py`/`route_sensitivity.py`: tooling dev,
bukan CI gate (CI tak punya akses Ollama).

Skor via RUBRIK deterministik (`core/eval_harness.py::evaluate_rubric`),
BUKAN LLM-judge — lihat alasan di docstring modul itu.

Pakai:
    python scripts/run_evals.py --path evals/dev              # semua kasus role dev
    python scripts/run_evals.py --path evals                  # semua role
    python scripts/run_evals.py --path evals/dev/basic.yaml    # satu file
    python scripts/run_evals.py --path evals/dev --model ollama:qwen2.5:3b
    python scripts/run_evals.py --path evals/dev --timeout 10  # approval/question timeout (detik)

Exit code 0 = semua lolos, 1 = ada yang gagal (untuk gating manual/CI
opsional yang punya akses Ollama sendiri).
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

# scripts/ tidak masuk package (lihat pyproject packages.find) → tambah root proyek
# ke path agar import absolut (core.*, infra.*) bekerja saat dijalankan dari mana pun.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent_loop import AgentConfig, AgentLoop  # noqa: E402
from core.eval_harness import EvalCase, evaluate_rubric, load_eval_cases  # noqa: E402
from infra.config import AppConfig  # noqa: E402
from infra.database import DatabaseManager  # noqa: E402
from infra.settings import SettingsStore  # noqa: E402

DEFAULT_PATH = "evals"
# Timeout approval/question pendek untuk eval — model yang mencoba ask_user
# atau tool butuh-approval tak boleh membuat satu kasus menggantung lama.
# `autopilot=True` (di bawah) sudah mencegah approval blocking; ini jaga-jaga
# untuk ask_user yang tetap menunggu QuestionGate (fail-soft, bukan approval).
DEFAULT_TIMEOUT_SEC = 5


async def _run_one_case(case: EvalCase, provider: str | None, model: str | None, timeout_sec: int):
    """Jalankan satu kasus lewat AgentLoop sungguhan di DB+workspace temporer.

    `autopilot=True` (§ AgentConfig): tool butuh-approval DIANTRI sebagai
    proposal, bukan menunggu manusia yang tak akan pernah ada di sini — tapi
    `Turn.tool_calls` tetap mencatat NIAT model memanggilnya (dicek `_execute_tool`
    SEBELUM approval diputuskan), jadi rubrik `tool_called`/`tool_not_called`
    tetap valid mengukur PILIHAN model, bukan hasil eksekusi tool.
    """
    workspace = tempfile.mkdtemp(prefix="openclawn-eval-")
    try:
        for rel_path, content in case.setup_files.items():
            target = Path(workspace) / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        config = AppConfig(db_path=":memory:", approval_timeout_sec=timeout_sec)
        db = DatabaseManager(config)
        conn = await db.conn()
        with open("migrations/001_initial.sql") as f:
            await conn.executescript(f.read())
        await conn.commit()

        if provider and model:
            await SettingsStore(db).set_model_override(provider, model)

        agent = AgentLoop(
            AgentConfig(
                role=case.role,
                session_id=f"eval-{case.name}",
                workspace_override=workspace,
                autopilot=True,
            ),
            db=db,
            # BUG nyata ditemukan lewat run sungguhan (2026-08-03): AgentLoop.__init__
            # default `config: AppConfig = CONFIG` (singleton GLOBAL) bila tak
            # diteruskan eksplisit — tanpa baris ini, `config` di atas (db_path
            # ":memory:", approval_timeout_sec pendek) dibuat tapi TAK PERNAH
            # dipakai AgentLoop, yang diam-diam memakai CONFIG global (fallback_chain,
            # approval_timeout_sec produksi, dst). Tak menyebabkan crash langsung
            # (db=db tetap benar untuk operasi utama), tapi cukup untuk membuat
            # perilaku eval TAK sesuai config yang dimaksud — kelas bug "diam-diam
            # salah", bukan "jelas gagal".
            config=config,
        )
        before_tasks = asyncio.all_tasks()
        async for _ in agent.run(case.input):
            pass  # tak butuh event streaming di sini — hasil akhir ada di agent.history

        # AgentLoop.run() menjadwalkan _post_turn sebagai background task
        # fire-and-forget (asyncio.create_task, lihat core/agent_loop.py) —
        # masih berjalan setelah generator run() habis.
        #
        # ROOT CAUSE DITEMUKAN & DIKONFIRMASI (2026-08-03, bukan diasumsikan):
        # timeout PENDEK di sini awalnya (10 detik) TIDAK CUKUP untuk
        # _post_turn's SENDIRI menyelesaikan cascade fallback LLM-nya (title
        # generation, _generate_session_title) — hingga 4 percobaan model ×
        # retry+backoff bisa makan >10 detik saat Ollama lambat/model default
        # (compaction_local_model) tak tersedia lokal. Direproduksi terisolasi:
        # tanpa timeout mencukupi, `await db.close()` di bawah jalan SEMENTARA
        # _post_turn case INI MASIH BERJALAN, lalu kasus BERIKUTNYA sudah mulai
        # — _post_turn yang telat itu kemudian menabrak DB yang sudah tertutup,
        # muncul sebagai `sqlite3.OperationalError: no such table: memory_l1`
        # (bukan "Cannot operate on a closed database" yang lebih jelas —
        # perilaku aiosqlite yang membingungkan saat close terjadi DI TENGAH
        # operasi yang sudah diantre, bukan sebelum operasi dimulai). Ini
        # murni bug skrip ini (timeout terlalu pendek + tak mengecek apakah
        # wait benar-benar selesai), BUKAN bug di core/agent_loop.py.
        #
        # Perbaikan: timeout jauh lebih longgar (60 detik — melebihi worst-case
        # realistis 4 model × ~3 percobaan × backoff), DAN cek eksplisit
        # apakah masih ada yang pending setelahnya — kalau ya, PERINGATKAN
        # keras alih-alih diam-diam menutup DB (yang akan mengulang bug ini).
        new_tasks = [t for t in asyncio.all_tasks() - before_tasks if not t.done()]
        if new_tasks:
            done, pending = await asyncio.wait(new_tasks, timeout=60)
            if pending:
                # DB SENGAJA TIDAK ditutup di sini — persis ini yang tadinya
                # menyebabkan "no such table" salah arah muncul di KASUS
                # BERIKUTNYA. Konsekuensinya: koneksi :memory: ini bocor
                # sampai proses Python keluar — dapat diterima untuk skrip
                # dev berumur pendek, jauh lebih baik daripada merusak task
                # yang masih berjalan. Kasus berikutnya tetap dapat db/agent
                # BARU (tak saling memengaruhi).
                print(
                    f"    PERINGATAN: {len(pending)} background task (mis. title "
                    f"generation) untuk kasus '{case.name}' belum selesai setelah "
                    "60 detik — kemungkinan Ollama lambat/tak sehat. Hasil kasus "
                    "ini tetap dinilai dari jawaban yang sudah ada; DB dibiarkan "
                    "terbuka (bukan ditutup paksa) untuk mencegah merusak task "
                    "itu — cek kesehatan Ollama sebelum melanjutkan.",
                    file=sys.stderr,
                )
                last_turn = agent.history[-1] if agent.history else None
                return evaluate_rubric(
                    case,
                    last_turn.content if last_turn else "",
                    last_turn.tool_calls if last_turn else [],
                )

        await db.close()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    if not agent.history:
        return evaluate_rubric(case, "", [])
    turn = agent.history[-1]
    return evaluate_rubric(case, turn.content, turn.tool_calls)


async def _main(path: str, provider: str | None, model: str | None, timeout_sec: int) -> int:
    cases = load_eval_cases(path)
    if not cases:
        print(f"Tidak ada kasus uji ditemukan di {path}")
        return 0

    print(f"Menjalankan {len(cases)} kasus uji dari {path}...")
    if provider and model:
        print(f"Model override: {provider}:{model}\n")
    else:
        print("Tanpa override — memakai SmartRouter otomatis per role.\n")

    passed = 0
    for case in cases:
        result = await _run_one_case(case, provider, model, timeout_sec)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {case.role}/{case.name}")
        if result.passed:
            passed += 1
        else:
            for failure in result.failures:
                print(f"    - {failure}")
            print(f"    jawaban: {result.answer_preview!r}")

    total = len(cases)
    print(f"\n{passed}/{total} lolos.")
    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--path", default=DEFAULT_PATH, help=f"File/direktori eval (default: {DEFAULT_PATH})"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override model 'provider:model' (mis. ollama:qwen2.5:3b). Default: router otomatis.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Timeout approval/question detik (default: {DEFAULT_TIMEOUT_SEC})",
    )
    args = parser.parse_args()

    provider, model = (None, None)
    if args.model:
        provider, _, model = args.model.partition(":")
        if not model:
            parser.error("--model harus berformat 'provider:model', mis. ollama:qwen2.5:3b")

    sys.exit(asyncio.run(_main(args.path, provider, model, args.timeout)))


if __name__ == "__main__":
    main()
