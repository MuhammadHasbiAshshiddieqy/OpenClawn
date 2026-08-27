"""Referensi harga model publik — untuk ESTIMASI RETROSPEKTIF (TODO.md § Prioritas
9.4), BUKAN untuk keputusan routing live.

MENGAPA TERPISAH DARI `SmartRouter.MODELS`: `cost_per_1k` di `MODELS` dan
`RouterConfigStore.get_map()` SENGAJA selalu `0.0` — lihat komentar "cost nyata
tak dipetakan; jangan tebak" di `core/router_config.py`. Itu keputusan yang
benar untuk keputusan routing LIVE (harga yang salah bisa membiaskan router
memilih model yang sebenarnya lebih mahal). Tapi akibatnya `routing_events.cost_usd`
SELALU 0.0 untuk setiap baris — tak ada data biaya nyata yang bisa diagregasi
untuk laporan retrospektif seperti dashboard penghematan.

Modul ini menutup gap itu dengan cara yang jujur: tabel harga publik yang
diberi TANGGAL VERIFIKASI eksplisit dan SUMBER, dipakai HANYA untuk estimasi
setelah fakta (bukan untuk mengubah perilaku routing). Setiap angka di
`MODEL_PRICING_USD_PER_1M` harus bisa ditelusuri ke halaman pricing resmi
provider — jangan tambah baris tanpa sumber terverifikasi.
"""

from datetime import date

# Tanggal verifikasi harga di bawah. CEK ULANG bila laporan mulai terasa jauh
# dari tanggal ini — harga API cloud berubah tanpa pemberitahuan proyek ini.
PRICING_VERIFIED_ON = date(2026, 8, 27)

# {model: (usd per 1 juta token INPUT, usd per 1 juta token OUTPUT)}
# Model yang TIDAK ADA di sini dianggap TAK DIKETAHUI oleh estimate_cost_usd()
# — dikeluarkan dari agregat pemanggil, TIDAK diasumsikan 0 atau ditebak
# (konsisten prinsip "jangan tebak" yang sama dengan router_config.py).
MODEL_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    # --- Ollama (self-hosted, lokal) — tanpa biaya API. ---
    "gemma4:e2b": (0.0, 0.0),
    "gemma4:e4b": (0.0, 0.0),
    "gemma4:12b": (0.0, 0.0),
    "deepseek-r1:latest": (0.0, 0.0),
    "qwen3.5:9b": (0.0, 0.0),
    "neural-chat:latest": (0.0, 0.0),
    # --- Gemini (Google AI Studio, harga standar <200K token/prompt) ---
    # Sumber: developer.puter.com/tutorials/gemini-api-pricing (Aug 2026),
    # dikonfirmasi silang beberapa tracker pricing independen lain.
    "gemini-2.5-flash": (0.150, 1.25),
    "gemini-2.5-pro": (1.25, 10.00),
    # --- Anthropic (platform.claude.com/docs/en/about-claude/pricing) ---
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Estimasi biaya dari tarif publik di atas.

    Ini BUKAN tagihan nyata — tidak memperhitungkan prompt caching (bisa
    memangkas biaya input hingga 90%), batch discount, kontrak enterprise
    kustom, atau kuota gratis tier tertentu. Selalu perlakukan hasilnya
    sebagai ESTIMASI, jangan tampilkan sebagai angka pasti (lihat
    `is_estimate` di `RoutingAuditor.cost_savings_report`).

    Return `None` bila `model` tak ada di tabel harga — caller HARUS
    mengeluarkan baris ini dari agregat, bukan memperlakukan None sebagai 0
    (model tak dikenal punya biaya tak diketahui, bukan biaya nol).
    """
    rate = MODEL_PRICING_USD_PER_1M.get(model)
    if rate is None:
        return None
    rate_in, rate_out = rate
    return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000
