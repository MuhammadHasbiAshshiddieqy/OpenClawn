"""Tests untuk ContextCompactor — token budget, history trimming, system prompt."""

from dataclasses import dataclass, field
from core.compactor import ContextCompactor, _estimate_tokens


@dataclass
class _FakeTurn:
    role: str
    content: str = ""
    tool_calls: list = field(default_factory=list)


# ── Token estimation ────────────────────────────────────────────────────────


def test_estimate_tokens_heuristic():
    """~4 karakter per token — heuristik kasar tapi konsisten."""
    assert _estimate_tokens("hello world") == 2  # 11 // 4
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("a" * 400) == 100


# ── System prompt building ───────────────────────────────────────────────────


def test_system_prompt_includes_soul():
    """Soul harus selalu ada sebagai elemen pertama system prompt."""
    c = ContextCompactor(max_tokens=1000)
    messages = c.build(soul="I am PM agent.", memory={}, history=[], user_message="hi")
    assert messages[0]["role"] == "system"
    assert "I am PM agent." in messages[0]["content"]


def test_system_prompt_includes_l1_state():
    """L1 memory (key-value state) harus masuk system prompt."""
    c = ContextCompactor(max_tokens=1000)
    memory = {"l1": {"last_summary": "finished auth module", "mode": "review"}}
    messages = c.build(soul="soul", memory=memory, history=[], user_message="hi")
    assert "last_summary" in messages[0]["content"]
    assert "finished auth module" in messages[0]["content"]


def test_system_prompt_includes_l2_facts():
    """L2 facts harus masuk system prompt, maksimal 10."""
    c = ContextCompactor(max_tokens=1000)
    memory = {"l2": [f"fact-{i}" for i in range(15)]}
    messages = c.build(soul="soul", memory=memory, history=[], user_message="hi")
    assert "fact-0" in messages[0]["content"]
    assert "fact-14" not in messages[0]["content"]  # di-limit ke 10


def test_system_prompt_includes_l3_skills():
    """L3 active skills harus masuk, maksimal 5."""
    c = ContextCompactor(max_tokens=1000)
    memory = {"l3": [{"skill_name": f"skill-{i}"} for i in range(8)]}
    messages = c.build(soul="soul", memory=memory, history=[], user_message="hi")
    assert "skill-0" in messages[0]["content"]
    assert "skill-7" not in messages[0]["content"]  # di-limit ke 5


def test_system_prompt_includes_l4_archives():
    """L4 past session summaries harus masuk, maksimal 3."""
    c = ContextCompactor(max_tokens=1000)
    memory = {"l4": [f"archive-{i}" for i in range(6)]}
    messages = c.build(soul="soul", memory=memory, history=[], user_message="hi")
    assert "archive-0" in messages[0]["content"]


def test_empty_memory_does_not_crash():
    """Memory kosong tidak boleh menyebabkan error."""
    c = ContextCompactor(max_tokens=1000)
    messages = c.build(soul="soul", memory={}, history=[], user_message="hi")
    assert len(messages) >= 2  # system + user


# ── Token budget trimming ────────────────────────────────────────────────────


def test_all_history_fits_within_budget():
    """Jika semua history muat dalam budget, tidak ada yang dipotong."""
    c = ContextCompactor(max_tokens=10_000)
    history = [
        _FakeTurn(role="user", content="short q"),
        _FakeTurn(role="assistant", content="short a"),
    ]
    messages = c.build(soul="soul", memory={}, history=history, user_message="hi")
    # system + 2 history + user = 4 messages
    assert len(messages) == 4


def test_history_trimmed_when_exceeds_budget():
    """History lama harus dipotong jika token budget habis."""
    c = ContextCompactor(max_tokens=100)  # budget sangat kecil
    long_history = [
        _FakeTurn(role="user", content="x" * 300),
        _FakeTurn(role="assistant", content="y" * 300),
        _FakeTurn(role="user", content="z" * 300),
        _FakeTurn(role="assistant", content="w" * 300),
    ]
    messages = c.build(soul="s", memory={}, history=long_history, user_message="hi")
    # Harusnya lebih sedikit dari 4 history turn + system + user
    assert len(messages) < 6  # < system + 4 + user


def test_most_recent_history_kept():
    """Turn paling baru harus dipertahankan, yang lama dipotong."""
    c = ContextCompactor(max_tokens=200)
    history = [
        _FakeTurn(role="user", content="first question " * 20),
        _FakeTurn(role="assistant", content="first answer " * 20),
        _FakeTurn(role="user", content="last question"),
        _FakeTurn(role="assistant", content="last answer"),
    ]
    messages = c.build(soul="s", memory={}, history=history, user_message="hi")

    # "last question" dan "last answer" harus tetap ada
    all_content = " ".join(m["content"] for m in messages)
    assert "last question" in all_content
    assert "last answer" in all_content


def test_user_message_always_present():
    """User message saat ini HARUS selalu ada, meskipun budget habis."""
    c = ContextCompactor(max_tokens=10)  # sangat kecil
    messages = c.build(soul="s", memory={}, history=[], user_message="critical")
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "critical"


def test_tool_turns_filtered_out():
    """Turn dengan role selain user/assistant (mis. tool) harus diabaikan."""
    c = ContextCompactor(max_tokens=10_000)
    history = [
        _FakeTurn(role="user", content="q"),
        _FakeTurn(role="tool", content="tool result"),
        _FakeTurn(role="assistant", content="a"),
    ]
    messages = c.build(soul="s", memory={}, history=history, user_message="hi")
    contents = [m["content"] for m in messages if m["role"] not in ("system",)]
    assert "tool result" not in contents


def test_history_capped_at_20_turns():
    """Maksimal 20 turn history dipertimbangkan (dari akhir)."""
    c = ContextCompactor(max_tokens=1_000_000)
    history = [_FakeTurn(role="user", content=f"msg-{i}") for i in range(30)]
    messages = c.build(soul="s", memory={}, history=history, user_message="final")
    # System + (maks 20 user) + final user
    assert len(messages) <= 22
    # msg-0 (paling awal) tidak boleh muncul, msg-29 (paling akhir) harus muncul
    all_text = " ".join(m["content"] for m in messages)
    assert "msg-0" not in all_text
    assert "msg-29" in all_text


# ── estimate_context_tokens (token budget meter §1.4) ─────────────────────────


def test_estimate_context_tokens_sums_all_messages():
    """Estimasi context = jumlah token semua message (heuristik ~4 char/token)."""
    c = ContextCompactor(max_tokens=1000)
    messages = [
        {"role": "system", "content": "a" * 400},  # 100 token
        {"role": "user", "content": "b" * 40},  # 10 token
    ]
    assert c.estimate_context_tokens(messages) == 110


def test_estimate_context_tokens_empty_and_missing_content():
    """Pesan kosong / tanpa 'content' tidak crash, dihitung 0."""
    c = ContextCompactor(max_tokens=1000)
    assert c.estimate_context_tokens([]) == 0
    assert c.estimate_context_tokens([{"role": "user"}]) == 0


def test_estimate_context_tokens_matches_build_output():
    """Estimasi konsisten dengan hasil build() — meter mencerminkan prompt nyata."""
    c = ContextCompactor(max_tokens=10_000)
    messages = c.build(soul="soul cukup panjang", memory={}, history=[], user_message="halo dunia")
    est = c.estimate_context_tokens(messages)
    assert est > 0
    assert est <= 10_000
