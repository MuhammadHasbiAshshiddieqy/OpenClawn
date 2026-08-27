"""Eval harness — regression test kualitas jawaban agent (TODO.md § Prioritas
8.2, riset kompetitor eve.dev 2026-08-03).

MENGAPA ADA: `core/crystallizer.py` (I3) menilai kualitas *skill* sebelum
disimpan, dan `calibration_report`/`role_report` (I1, `core/audit.py`)
mengukur akurasi *routing* dari koreksi user nyata — tapi sebelum modul ini,
tak ada cara sistematis menjawab "apakah perubahan prompt/model/router
menurunkan kualitas JAWABAN agent" (mirip promptfoo/evals). Perubahan seperti
itu sebelumnya cuma bisa diketahui salah lewat laporan user setelah fakta.

DESAIN SENGAJA SEDERHANA (CLAUDE.md §8): skor via RUBRIK deterministik
(substring/tool-called/panjang minimum), BUKAN LLM-judge. Alasan: LLM-judge
menambah non-determinisme DAN biaya (satu LLM call lagi per kasus uji) untuk
manfaat yang belum terbukti perlu — kalau nanti rubrik sederhana terbukti
tak cukup, LLM-judge bisa ditambah SEBAGAI mode terpisah, TUNDUK aturan
evaluator≥generator (I3, `core/crystallizer.py::EVALUATOR_FOR`) — jangan
tambahkan LLM-judge tanpa aturan itu diperiksa ulang.

PEMISAHAN SENGAJA dari `scripts/run_evals.py`: modul ini HANYA logika murni
(parsing YAML, evaluasi rubrik) — TIDAK menyentuh `AgentLoop`/LLM sama
sekali, supaya bisa diuji lewat pytest biasa TANPA melanggar CLAUDE.md §5
("LLM: SELALU mock, test tidak boleh memanggil Ollama/Claude sungguhan").
Menjalankan kasus uji lewat agent SUNGGUHAN (butuh Ollama nyala) ada di
`scripts/run_evals.py`, di luar suite pytest — sama pola `scripts/seed_routing.py`
(data buatan, bukan bagian CI).
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class EvalCase:
    """Satu kasus uji: input untuk agent + kriteria rubrik yang harus dipenuhi
    jawabannya. Dimuat dari satu file YAML (`evals/<role>/*.yaml`)."""

    name: str
    role: str
    input: str
    setup_files: dict[str, str] = field(default_factory=dict)  # {path_relatif: isi} di workspace
    expect: dict = field(default_factory=dict)
    source_path: str = ""


@dataclass
class EvalResult:
    """Hasil satu kasus uji setelah dijalankan lewat agent sungguhan."""

    case_name: str
    passed: bool
    failures: list[str]
    tool_calls: list[str]
    answer_preview: str


# Kunci `expect` yang didukung. Menambah kunci baru = menambah cabang di
# evaluate_rubric() — daftar ini HARUS tetap sinkron (dicek test).
SUPPORTED_EXPECT_KEYS = frozenset(
    {"contains", "not_contains", "tool_called", "tool_not_called", "min_length"}
)


def load_eval_cases(path: str) -> list[EvalCase]:
    """Muat kasus uji dari satu file YAML, atau semua `*.yaml` di satu direktori
    (rekursif satu level — `evals/<role>/*.yaml`).

    Tiap file YAML berisi SATU LIST kasus uji (bukan satu kasus per file) —
    memudahkan mengelompokkan kasus terkait dalam satu file bertema. `role`
    kasus diambil dari NAMA FOLDER induk file (`evals/dev/x.yaml` → role
    `"dev"`), bukan field eksplisit di YAML — menghindari duplikasi/typo
    antara nama folder dan isi file.
    """
    p = Path(path)
    files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    cases: list[EvalCase] = []
    for file in files:
        role = file.parent.name
        with open(file, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or []
        if not isinstance(raw, list):
            raise ValueError(f"{file}: harus berisi list kasus uji, dapat {type(raw).__name__}")
        for i, entry in enumerate(raw):
            name = entry.get("name") or f"{file.stem}-{i}"
            cases.append(
                EvalCase(
                    name=name,
                    role=role,
                    input=entry["input"],
                    setup_files=entry.get("setup_files", {}) or {},
                    expect=entry.get("expect", {}) or {},
                    source_path=str(file),
                )
            )
    return cases


def evaluate_rubric(case: EvalCase, answer: str, tool_calls: list[dict]) -> EvalResult:
    """Cek jawaban agent (`answer`) + tool yang dipanggil (`tool_calls`, list
    `{"name", "input"}` dari `Turn.tool_calls`) terhadap `case.expect`.

    Pure function — TIDAK melakukan I/O atau memanggil LLM, jadi bisa diuji
    penuh dengan data buatan (`tests/test_eval_harness.py`).
    """
    expect = case.expect
    failures: list[str] = []
    answer_lower = answer.lower()
    called_names = [tc["name"] for tc in tool_calls]

    for want in expect.get("contains", []):
        if want.lower() not in answer_lower:
            failures.append(f"jawaban tidak mengandung: {want!r}")

    for unwanted in expect.get("not_contains", []):
        if unwanted.lower() in answer_lower:
            failures.append(f"jawaban mengandung frasa terlarang: {unwanted!r}")

    for tool_name in expect.get("tool_called", []):
        if tool_name not in called_names:
            failures.append(
                f"tool wajib dipanggil tapi tidak: {tool_name!r} (dipanggil: {called_names})"
            )

    for tool_name in expect.get("tool_not_called", []):
        if tool_name in called_names:
            failures.append(f"tool terlarang dipanggil: {tool_name!r}")

    min_length = expect.get("min_length")
    if min_length is not None and len(answer) < min_length:
        failures.append(f"jawaban terlalu pendek: {len(answer)} < {min_length} karakter")

    return EvalResult(
        case_name=case.name,
        passed=len(failures) == 0,
        failures=failures,
        tool_calls=called_names,
        answer_preview=answer[:200],
    )
