"""Test compaction headroom (terinspirasi chopratejas/headroom).

Compaction = ringkas turn lama jadi satu blok alih-alih MEMBUANG saat budget habis.
Yang kritis:
  - Opt-in: mode 'off' → tak menyentuh history (truncation lama, default aman).
  - Hemat tanpa kehilangan total: turn lama → 1 ringkasan, turn terbaru UTUH.
  - Fail-safe (§1.3): summarizer error/kosong → history asli, tak pernah crash turn.
  - Idempoten: blok yang sudah ringkasan tak diringkas lagi.

Summarizer di-inject (seam bersih, §5) → tak butuh LLM nyata.
"""

from dataclasses import dataclass

import pytest

from core.compactor import COMPACTION_MARKER, ContextCompactor
from infra.config import AppConfig
from infra.settings import SettingsStore
from infra.database import DatabaseManager


@dataclass
class FakeTurn:
    role: str
    content: str = ""


async def _summ_ok(joined: str) -> str:
    return "RINGKASAN: " + joined[:20]


async def _summ_empty(joined: str) -> str:
    return "   "


async def _summ_boom(joined: str) -> str:
    raise RuntimeError("llm down")


def _long_history(n: int, chars: int = 4000) -> list[FakeTurn]:
    """n turn besar agar pasti melebihi budget kecil."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(FakeTurn(role=role, content=f"turn {i} " + "x" * chars))
    return out


# ── ContextCompactor.compact ──────────────────────────────────────────────────


async def test_no_compaction_when_history_fits():
    """History muat di budget → tak ada LLM call, history utuh."""
    comp = ContextCompactor(max_tokens=28_000)
    hist = [FakeTurn("user", "halo"), FakeTurn("assistant", "hai")]
    out = await comp.compact(hist, _summ_ok, keep_recent=4, min_old_turns=1)
    assert out == hist  # identik, tak diubah


async def test_compaction_summarizes_old_keeps_recent():
    """Budget kecil + history panjang → turn lama jadi 1 ringkasan, recent UTUH."""
    comp = ContextCompactor(max_tokens=2_000)  # kecil → pasti overflow
    hist = _long_history(10)
    out = await comp.compact(hist, _summ_ok, keep_recent=3, min_old_turns=3)
    # 1 ringkasan + 3 turn terbaru.
    assert len(out) == 4
    assert out[0].content.startswith(COMPACTION_MARKER)
    # 3 terakhir sama persis dengan history asli (tak diringkas).
    assert [t.content for t in out[1:]] == [t.content for t in hist[-3:]]


async def test_compaction_off_via_min_old_turns():
    """Turn lama terlalu sedikit (< min_old_turns) → tak diringkas."""
    comp = ContextCompactor(max_tokens=500)
    hist = _long_history(5)
    out = await comp.compact(hist, _summ_ok, keep_recent=4, min_old_turns=3)
    # old = 1 turn < min 3 → kembali apa adanya.
    assert out == hist


async def test_summarizer_error_falls_back_to_original():
    """Summarizer error → history asli (truncation aman), tak crash."""
    comp = ContextCompactor(max_tokens=1_000)
    hist = _long_history(10)
    out = await comp.compact(hist, _summ_boom, keep_recent=3, min_old_turns=3)
    assert out == hist


async def test_empty_summary_falls_back():
    """Ringkasan kosong → jangan ganti history dengan blok kosong."""
    comp = ContextCompactor(max_tokens=1_000)
    hist = _long_history(10)
    out = await comp.compact(hist, _summ_empty, keep_recent=3, min_old_turns=3)
    assert out == hist


async def test_idempotent_does_not_recompact():
    """History yang sudah punya blok ringkasan tak diringkas lagi."""
    comp = ContextCompactor(max_tokens=1_000)
    hist = [
        FakeTurn("assistant", f"{COMPACTION_MARKER} ringkasan lama"),
        *_long_history(8),
    ]
    out = await comp.compact(hist, _summ_ok, keep_recent=3, min_old_turns=3)
    # old (yang akan diringkas) sudah mengandung marker → skip, kembali apa adanya.
    assert out == hist


# ── SettingsStore.get/set_compaction_mode ─────────────────────────────────────


@pytest.fixture
async def db():
    manager = DatabaseManager(AppConfig(db_path=":memory:"))
    with open("migrations/001_initial.sql") as f:
        sql = f.read()
    conn = await manager.conn()
    await conn.executescript(sql)
    await conn.commit()
    yield manager
    await manager.close()


async def test_compaction_mode_default_off(db):
    store = SettingsStore(db)
    assert await store.get_compaction_mode() == "off"


async def test_compaction_mode_roundtrip(db):
    store = SettingsStore(db)
    await store.set_compaction_mode("local")
    assert await store.get_compaction_mode() == "local"
    await store.set_compaction_mode("cloud")
    assert await store.get_compaction_mode() == "cloud"


async def test_compaction_mode_invalid_falls_back(db):
    """Nilai tak dikenal → fail-safe ke off (tak diam-diam menyalakan LLM call)."""
    store = SettingsStore(db)
    await store.set_compaction_mode("bogus")
    assert await store.get_compaction_mode() == "off"
