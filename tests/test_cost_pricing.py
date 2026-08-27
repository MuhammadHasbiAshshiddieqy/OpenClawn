"""Tests untuk core/cost_pricing.py — estimasi biaya retrospektif (§ Prioritas 9.4)."""

from core.cost_pricing import MODEL_PRICING_USD_PER_1M, estimate_cost_usd


def test_ollama_models_are_free():
    """Model self-hosted (Ollama) harus tarif 0.0 — dijalankan lokal, bukan API."""
    assert estimate_cost_usd("gemma4:e4b", 1_000_000, 1_000_000) == 0.0
    assert estimate_cost_usd("deepseek-r1:latest", 500_000, 500_000) == 0.0


def test_gemini_flash_pricing():
    """$0.15 input / $1.25 output per 1M token (tarif publik, lihat modul)."""
    cost = estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == 0.150 + 1.25


def test_gemini_pro_pricing():
    cost = estimate_cost_usd("gemini-2.5-pro", 1_000_000, 1_000_000)
    assert cost == 1.25 + 10.00


def test_claude_pricing():
    cost = estimate_cost_usd("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == 1.00 + 5.00


def test_zero_tokens_is_zero_cost():
    assert estimate_cost_usd("gemini-2.5-pro", 0, 0) == 0.0


def test_unknown_model_returns_none_not_zero():
    """Model tak dikenal HARUS None, bukan 0.0 — biaya tak diketahui ≠ gratis
    (prinsip 'jangan tebak' yang sama dengan router_config.py)."""
    assert estimate_cost_usd("some-future-model-not-in-table", 1000, 1000) is None


def test_partial_token_ratio_scales_correctly():
    """Input jauh lebih murah dari output di semua model cloud — pastikan
    keduanya dikalikan tarif masing-masing, bukan tarif rata-rata."""
    cost_input_heavy = estimate_cost_usd("gemini-2.5-pro", 2_000_000, 0)
    cost_output_heavy = estimate_cost_usd("gemini-2.5-pro", 0, 2_000_000)
    assert cost_input_heavy == 2.50  # 2M * 1.25/1M
    assert cost_output_heavy == 20.00  # 2M * 10.00/1M
    assert cost_output_heavy > cost_input_heavy


def test_every_pricing_entry_is_non_negative_tuple_of_two():
    """Sanity check struktural tabel harga — cegah typo (mis. satu nilai
    tunggal alih-alih tuple input/output) lolos tanpa ketahuan."""
    for model, rate in MODEL_PRICING_USD_PER_1M.items():
        assert isinstance(rate, tuple) and len(rate) == 2, model
        assert rate[0] >= 0 and rate[1] >= 0, model
