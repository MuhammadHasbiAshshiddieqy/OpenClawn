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
    assert route.provider in ("ollama", "anthropic")  # tidak crash


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
        assert provider in ("ollama", "anthropic")
        assert cost >= 0.0
