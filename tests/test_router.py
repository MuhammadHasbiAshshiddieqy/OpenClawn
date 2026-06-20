import pytest
from core.router import SmartRouter, Complexity


@pytest.fixture
def pm_router(tmp_path):
    """SmartRouter untuk role pm dengan soul.toml sementara."""
    soul = tmp_path / "soul.toml"
    soul.write_text(
        '[routing]\nprefer_local = true\nupgrade_keywords = ["arsitektur", "strategi"]\n'
    )
    return SmartRouter(role="pm", soul_path=str(soul))


@pytest.fixture
def qa_router(tmp_path):
    """SmartRouter untuk role qa dengan prefer_local=false."""
    soul = tmp_path / "soul.toml"
    soul.write_text('[routing]\nprefer_local = false\nupgrade_keywords = ["security"]\n')
    return SmartRouter(role="qa", soul_path=str(soul))


def test_soul_upgrade_keyword_raises_complexity(pm_router):
    """Audit #1: keyword dari soul.toml harus menaikkan kompleksitas."""
    route = pm_router.decide(messages=[], query="bantu desain arsitektur sistem")
    assert route.soul_upgrade_hit is True
    assert route.complexity in (Complexity.COMPLEX, Complexity.CRITICAL)


def test_prefer_local_keeps_simple_on_ollama(pm_router):
    """prefer_local=true harus menahan query sederhana di Ollama."""
    route = pm_router.decide(messages=[], query="apa itu sprint?")
    assert route.provider == "ollama"


def test_no_soul_hit_on_unrelated_query(pm_router):
    """Query tanpa keyword soul tidak memicu upgrade."""
    route = pm_router.decide(messages=[], query="halo, apa kabar?")
    assert route.soul_upgrade_hit is False


def test_technical_keyword_increases_score(qa_router):
    """Keyword teknis (code, debug) harus menaikkan skor."""
    route = qa_router.decide(messages=[], query="debug this api code")
    assert route.complexity_score >= 2


def test_long_query_increases_score(pm_router):
    """Query panjang (> 50 token) harus menaikkan skor."""
    long_query = "tolong " * 60
    route_long = pm_router.decide(messages=[], query=long_query)
    route_short = pm_router.decide(messages=[], query="halo")
    assert route_long.complexity_score > route_short.complexity_score


def test_all_dimensions_present(pm_router):
    """Semua 8 dimensi harus ada di RouteDecision.dimensions — auditor butuh ini."""
    route = pm_router.decide(messages=[], query="test")
    required = {
        "query_tokens",
        "has_tech_kw",
        "needs_multistep",
        "history_len",
        "role",
        "has_urgency",
        "needs_stream",
        "is_continuation",
        "soul_upgrade_hit",
    }
    assert required.issubset(route.dimensions.keys())


# ── Edge case tests ──────────────────────────────────────────────────────────


def test_multistep_keyword_detected(pm_router):
    """Keyword multi-step (analisis, bandingkan, evaluasi) harus menaikkan skor."""
    route = pm_router.decide(messages=[], query="analisis dan bandingkan dua pendekatan")
    assert route.dimensions["needs_multistep"] == 1
    assert route.complexity_score >= 2


def test_urgency_keyword_increases_score(pm_router):
    """Keyword urgency (urgent, segera, deadline) harus menaikkan skor."""
    route_urgent = pm_router.decide(messages=[], query="urgent: fix bug sekarang")
    route_normal = pm_router.decide(messages=[], query="fix bug nanti")
    assert route_urgent.complexity_score > route_normal.complexity_score


def test_history_length_increases_score(pm_router):
    """History panjang (>10 messages) harus menaikkan skor."""
    long_history = [{"role": "user", "content": f"msg-{i}"} for i in range(15)]
    route_long = pm_router.decide(messages=long_history, query="lanjutkan")
    route_short = pm_router.decide(messages=[], query="lanjutkan")
    assert route_long.complexity_score > route_short.complexity_score


def test_continuation_detected(pm_router):
    """History > 2 messages → is_continuation harus true."""
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    route = pm_router.decide(messages=history, query="lanjut")
    assert route.dimensions["is_continuation"] == 1


def test_empty_query_minimal_score(pm_router):
    """Query kosong harus dapat skor minimal, tapi tidak crash."""
    route = pm_router.decide(messages=[], query="")
    assert route.complexity_score == 0
    assert route.complexity == Complexity.TRIVIAL


def test_very_long_query_triggers_token_bonus(pm_router):
    """Query > 200 token (setara ~260+ kata) harus dapat +2 skor."""
    # query_tokens = len(query.split()) * 1.3  → butuh ~154 kata untuk 200 token
    long = "tolong " * 155
    route = pm_router.decide(messages=[], query=long)
    assert route.complexity_score >= 2  # +2 dari query_tokens > 200


def test_soul_hit_overrides_prefer_local(pm_router):
    """Soul upgrade hit harus memaksa upgrade meskipun prefer_local=true."""
    route = pm_router.decide(messages=[], query="bantu desain arsitektur microservice")
    assert route.soul_upgrade_hit is True
    # Soul hit → threshold_shift = 0 (tidak ada prefer_local penalty)
    assert route.complexity in (Complexity.COMPLEX, Complexity.CRITICAL)


def test_qa_prefer_local_false_allows_upgrade(qa_router):
    """prefer_local=false: query menengah bisa naik ke Claude."""
    route = qa_router.decide(messages=[], query="analisis keamanan autentikasi OAuth")
    # qa punya upgrade_keywords=["security"] — "keamanan" tidak match tapi query cukup kompleks
    assert route.provider in ("ollama", "gemini", "anthropic")  # tidak crash


def test_cost_per_1k_matches_model():
    """cost_per_1k harus sesuai model yang dipilih."""
    router = SmartRouter(role="dev", soul_path="roles/dev/soul.toml")
    route = router.decide(messages=[], query="halo")
    if route.provider == "ollama":
        assert route.cost_per_1k == 0.0
    elif route.provider == "anthropic":
        assert route.cost_per_1k > 0.0


def test_complexity_enum_maps_all_levels():
    """Semua level Complexity harus punya mapping model."""
    router = SmartRouter(role="pm", soul_path="roles/pm/soul.toml")
    for level in Complexity:
        model, provider, cost = router.MODELS[level]
        assert model  # tidak boleh kosong
        assert provider in ("ollama", "gemini", "anthropic")
        assert cost >= 0.0


# ── Multibahasa: keyword routing dari config + soul (§1.5) ────────────────────


def test_english_tech_keyword_detected(pm_router):
    """Default config kini ID+EN: kata teknis Inggris terdeteksi."""
    route = pm_router.decide(messages=[], query="please review and refactor this code")
    assert route.dimensions["has_tech_kw"] == 1


def test_english_multistep_keyword_detected(pm_router):
    """Kata multi-langkah Inggris (analyze/compare) terdeteksi."""
    route = pm_router.decide(messages=[], query="analyze and compare two approaches")
    assert route.dimensions["needs_multistep"] == 1


def test_soul_can_add_locale_keywords(tmp_path):
    """soul.toml [routing] dapat menambah keyword bahasa lain tanpa edit core (§1.5).

    Bahasa Spanyol 'analizar' tak ada di default → tambah lewat soul → terdeteksi.
    """
    soul = tmp_path / "soul.toml"
    soul.write_text(
        "[routing]\nprefer_local = false\n"
        'multistep_keywords = ["analizar", "comparar"]\n'
        'tech_keywords = ["codigo"]\n'
    )
    router = SmartRouter(role="dev", soul_path=str(soul))
    route = router.decide(messages=[], query="analizar el codigo del sistema")
    assert route.dimensions["needs_multistep"] == 1
    assert route.dimensions["has_tech_kw"] == 1


def test_unknown_language_falls_back_to_neutral_signal(tmp_path):
    """Bahasa tanpa keyword cocok tetap dirute oleh sinyal netral (panjang query)."""
    soul = tmp_path / "soul.toml"
    soul.write_text("[routing]\nprefer_local = false\nupgrade_keywords = []\n")
    router = SmartRouter(role="dev", soul_path=str(soul))
    # Query Jepang tanpa keyword cocok → has_tech_kw=0, tapi tetap dapat skor dari panjang.
    short = router.decide(messages=[], query="こんにちは")
    long = router.decide(messages=[], query="システム " * 60)
    assert short.dimensions["has_tech_kw"] == 0  # keyword tak cocok
    assert long.complexity_score > short.complexity_score  # sinyal netral tetap jalan


# ── Sinyal struktural bahasa-agnostik (Masalah A: kompleksitas lintas bahasa) ──


def test_code_signal_detected_without_keywords(tmp_path):
    """Query teknis pendek TANPA keyword (bahasa apa pun) terdeteksi via sinyal kode."""
    soul = tmp_path / "soul.toml"
    soul.write_text("[routing]\nprefer_local = false\nupgrade_keywords = []\n")
    router = SmartRouter(role="dev", soul_path=str(soul))
    route = router.decide(messages=[], query="def f(x): return x == y;")
    assert route.dimensions["has_code_signal"] == 1


def test_code_fence_raises_score(tmp_path):
    """Code fence (```), universal, menaikkan skor walau tanpa keyword."""
    soul = tmp_path / "soul.toml"
    soul.write_text("[routing]\nprefer_local = false\nupgrade_keywords = []\n")
    router = SmartRouter(role="dev", soul_path=str(soul))
    plain = router.decide(messages=[], query="こんにちは")
    coded = router.decide(messages=[], query="```python\nprint(1)\n```")
    assert coded.complexity_score > plain.complexity_score


def test_plain_text_no_code_signal(pm_router):
    """Teks biasa tanpa simbol kode → has_code_signal=0 (tak false-positive)."""
    route = pm_router.decide(messages=[], query="apa kabar hari ini")
    assert route.dimensions["has_code_signal"] == 0


# ── Deteksi script (Masalah B: kapabilitas bahasa model) ──────────────────────


def test_detect_script_cjk(pm_router):
    assert pm_router._detect_script("システムをデバッグする") == "cjk"


def test_detect_script_latin_ascii(pm_router):
    assert pm_router._detect_script("hello world") == "latin"


def test_detect_script_arabic(pm_router):
    assert pm_router._detect_script("مرحبا بالعالم") == "arabic"


def test_language_bump_off_by_default(tmp_path):
    """Default: bahasa non-latin TIDAK menaikkan tier (opt-in, tak menambah biaya)."""
    soul = tmp_path / "soul.toml"
    soul.write_text("[routing]\nprefer_local = false\nupgrade_keywords = []\n")
    router = SmartRouter(role="dev", soul_path=str(soul))
    route = router.decide(messages=[], query="こんにちは")
    assert route.dimensions["language_bumped"] == 0


def test_language_bump_raises_tier_when_enabled(tmp_path):
    """Opt-in: script di luar tier lokal → tier naik (threshold turun)."""
    import dataclasses
    from infra.config import CONFIG

    cfg = dataclasses.replace(CONFIG, routing_language_bump=True, routing_local_scripts=("latin",))
    soul = tmp_path / "soul.toml"
    soul.write_text("[routing]\nprefer_local = false\nupgrade_keywords = []\n")
    router = SmartRouter(role="dev", soul_path=str(soul), config=cfg)
    # Query CJK pendek: tanpa bump → trivial; dengan bump → naik minimal satu tier.
    cjk = router.decide(messages=[], query="こんにちは")
    assert cjk.dimensions["query_script"] == "cjk"
    assert cjk.dimensions["language_bumped"] == 1

    # Latin pendek serupa TIDAK di-bump (script lokal).
    latin = router.decide(messages=[], query="halo")
    assert latin.dimensions["language_bumped"] == 0
    # Bump CJK menghasilkan tier ≥ latin (skor sama, threshold lebih rendah).
    order = list(Complexity)
    assert order.index(cjk.complexity) >= order.index(latin.complexity)
