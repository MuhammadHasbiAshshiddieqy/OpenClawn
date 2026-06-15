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
