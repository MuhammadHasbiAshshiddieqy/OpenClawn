"""Tests untuk core/eval_harness.py (TODO.md § Prioritas 8.2, eval harness).

SEMUA test di sini murni logika (parsing YAML, evaluasi rubrik) — TIDAK
memanggil LLM/AgentLoop sama sekali, konsisten CLAUDE.md §5. Menjalankan
kasus uji lewat agent sungguhan ada di `scripts/run_evals.py`, di luar
suite pytest.
"""

from core.eval_harness import (
    SUPPORTED_EXPECT_KEYS,
    EvalCase,
    evaluate_rubric,
    load_eval_cases,
)


def _case(**expect) -> EvalCase:
    return EvalCase(name="t", role="dev", input="q", expect=expect)


# ── evaluate_rubric — contains/not_contains ─────────────────────────────────


def test_contains_passes_when_present():
    result = evaluate_rubric(_case(contains=["halo"]), "Halo, apa kabar?", [])
    assert result.passed is True
    assert result.failures == []


def test_contains_is_case_insensitive():
    result = evaluate_rubric(_case(contains=["HALO"]), "halo dunia", [])
    assert result.passed is True


def test_contains_fails_when_missing():
    result = evaluate_rubric(_case(contains=["fibonacci"]), "Ini jawaban lain", [])
    assert result.passed is False
    assert "fibonacci" in result.failures[0]


def test_not_contains_fails_when_present():
    result = evaluate_rubric(
        _case(not_contains=["maaf saya tidak bisa"]), "Maaf saya tidak bisa membantu", []
    )
    assert result.passed is False


def test_not_contains_passes_when_absent():
    result = evaluate_rubric(_case(not_contains=["error"]), "Berhasil dilakukan", [])
    assert result.passed is True


# ── evaluate_rubric — tool_called/tool_not_called ───────────────────────────


def test_tool_called_passes_when_present():
    result = evaluate_rubric(
        _case(tool_called=["file_read"]), "isi file", [{"name": "file_read", "input": {}}]
    )
    assert result.passed is True
    assert result.tool_calls == ["file_read"]


def test_tool_called_fails_when_absent():
    result = evaluate_rubric(_case(tool_called=["file_read"]), "jawaban tanpa baca file", [])
    assert result.passed is False
    assert "file_read" in result.failures[0]


def test_tool_not_called_fails_when_present():
    result = evaluate_rubric(
        _case(tool_not_called=["code_run"]), "sudah dijalankan", [{"name": "code_run", "input": {}}]
    )
    assert result.passed is False


def test_tool_not_called_passes_when_absent():
    result = evaluate_rubric(_case(tool_not_called=["code_run"]), "jawaban aman", [])
    assert result.passed is True


# ── evaluate_rubric — min_length ─────────────────────────────────────────────


def test_min_length_fails_when_too_short():
    result = evaluate_rubric(_case(min_length=50), "pendek", [])
    assert result.passed is False


def test_min_length_passes_when_long_enough():
    result = evaluate_rubric(_case(min_length=5), "cukup panjang", [])
    assert result.passed is True


def test_min_length_absent_by_default_never_fails():
    """Tanpa min_length di expect, jawaban kosong sekalipun tidak gagal
    KARENA aturan ini — kriteria lain (contains dst) yang menentukan."""
    result = evaluate_rubric(_case(), "", [])
    assert result.passed is True


# ── evaluate_rubric — kombinasi & pesan kegagalan ───────────────────────────


def test_multiple_failures_all_reported_not_just_first():
    result = evaluate_rubric(
        _case(contains=["a"], tool_called=["file_read"], min_length=100), "b", []
    )
    assert result.passed is False
    assert len(result.failures) == 3  # contains + tool_called + min_length, semua dilaporkan


def test_no_expect_criteria_always_passes():
    """Kasus tanpa `expect` sama sekali (mis. cuma smoke-test agent tak crash)
    HARUS lolos — tak ada kriteria berarti tak ada yang bisa gagal."""
    result = evaluate_rubric(_case(), "apa pun jawabannya", [{"name": "any_tool", "input": {}}])
    assert result.passed is True
    assert result.failures == []


def test_answer_preview_truncated_to_200_chars():
    long_answer = "x" * 500
    result = evaluate_rubric(_case(), long_answer, [])
    assert len(result.answer_preview) == 200


# ── load_eval_cases ───────────────────────────────────────────────────────────


def test_load_eval_cases_from_single_file(tmp_path):
    f = tmp_path / "basic.yaml"
    f.write_text(
        """
- name: case-a
  input: "halo"
  expect:
    contains: ["hai"]
- name: case-b
  input: "test"
"""
    )
    cases = load_eval_cases(str(f))
    assert len(cases) == 2
    assert cases[0].name == "case-a"
    assert cases[0].input == "halo"
    assert cases[0].expect == {"contains": ["hai"]}
    assert cases[1].expect == {}


def test_load_eval_cases_role_derived_from_parent_folder_name(tmp_path):
    role_dir = tmp_path / "dev"
    role_dir.mkdir()
    (role_dir / "x.yaml").write_text("- name: c1\n  input: q\n")

    cases = load_eval_cases(str(role_dir))
    assert cases[0].role == "dev"


def test_load_eval_cases_from_directory_loads_all_yaml_files(tmp_path):
    role_dir = tmp_path / "pm"
    role_dir.mkdir()
    (role_dir / "a.yaml").write_text("- name: c1\n  input: q1\n")
    (role_dir / "b.yaml").write_text("- name: c2\n  input: q2\n")

    cases = load_eval_cases(str(role_dir))
    names = {c.name for c in cases}
    assert names == {"c1", "c2"}


def test_load_eval_cases_missing_name_falls_back_to_filename_index(tmp_path):
    f = tmp_path / "unnamed.yaml"
    f.write_text("- input: q\n")

    cases = load_eval_cases(str(f))
    assert cases[0].name == "unnamed-0"


def test_load_eval_cases_setup_files_defaults_to_empty_dict(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("- name: c\n  input: q\n")

    cases = load_eval_cases(str(f))
    assert cases[0].setup_files == {}


def test_load_eval_cases_rejects_non_list_yaml(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("input: q\n")  # dict, bukan list

    try:
        load_eval_cases(str(f))
        raise AssertionError("harus raise ValueError untuk YAML bukan list")
    except ValueError as e:
        assert "list" in str(e)


# ── Sanity check struktural ──────────────────────────────────────────────────


def test_supported_expect_keys_matches_what_evaluate_rubric_actually_checks():
    """Cegah drift diam-diam antara SUPPORTED_EXPECT_KEYS (dokumentasi) dan
    kunci yang benar-benar dicek evaluate_rubric()."""
    case = _case(**{k: [] for k in SUPPORTED_EXPECT_KEYS if k != "min_length"})
    case.expect["min_length"] = 0
    # Tidak boleh raise KeyError/TypeError untuk kunci mana pun yang didaftarkan.
    evaluate_rubric(case, "jawaban", [])
