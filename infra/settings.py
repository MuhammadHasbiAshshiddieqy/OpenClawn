"""Runtime settings yang bisa diubah lewat /settings tanpa restart.

Saat ini: override model — memaksa SEMUA routing ke satu (provider, model),
melewati keputusan otomatis SmartRouter. Override adalah *pilihan sadar* untuk
eksperimen/development; router cerdas tetap default jika override kosong.

Murni di atas DatabaseManager (CLAUDE.md §1.6) — tabel app_settings key-value.
"""

from infra.database import DatabaseManager
from infra.i18n import DEFAULT_LOCALE, LOCALES

# Pilihan model yang diketahui, untuk dropdown /settings.
# Bukan pembatas keras — hanya saran tampilan. (provider, model, label).
KNOWN_MODELS: list[tuple[str, str, str]] = [
    # Lokal (Ollama) — setup utama, urut per kapasitas (selaras tier router).
    ("ollama", "gemma4:e4b", "Gemma4 e4b (lokal, ringan — TRIVIAL)"),
    ("ollama", "deepseek-r1:latest", "DeepSeek-R1 (lokal, reasoning — SIMPLE)"),
    ("ollama", "qwen3.5:9b", "Qwen3.5 9B (lokal, paling mampu — MODERATE)"),
    ("ollama", "neural-chat:latest", "Neural Chat (lokal, cadangan)"),
    ("ollama", "qwen2.5-coder:latest", "Qwen2.5 Coder (lokal, tool-capable)"),
    # Cloud — hanya untuk tier berat / eksperimen.
    ("gemini", "gemini-2.5-flash", "Gemini 2.5 Flash (cloud)"),
    ("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro (cloud)"),
    ("gemini", "gemini-2.0-flash", "Gemini 2.0 Flash (cloud)"),
    ("anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5 (cloud)"),
    ("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6 (cloud)"),
]

_KEY_PROVIDER = "model_override_provider"
_KEY_MODEL = "model_override_model"
_KEY_COMPACTION = "compaction_mode"
_KEY_UI_LOCALE = "ui_locale"

# Mode compaction yang valid (lihat core/compactor.py). off = truncation lama (aman).
COMPACTION_MODES = ("off", "local", "cloud")


class SettingsStore:
    """Baca/tulis setting runtime. Override model = (provider, model) atau None."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM app_settings WHERE key=?", (key,))
        return row["value"] if row else None

    async def set(self, key: str, value: str | None) -> None:
        if value is None or value == "":
            await self.db.execute("DELETE FROM app_settings WHERE key=?", (key,))
            return
        await self.db.execute(
            """INSERT INTO app_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (key, value),
        )

    async def get_model_override(self) -> tuple[str, str] | None:
        """Return (provider, model) jika override aktif, None jika router otomatis."""
        provider = await self.get(_KEY_PROVIDER)
        model = await self.get(_KEY_MODEL)
        if provider and model:
            return provider, model
        return None

    async def set_model_override(self, provider: str | None, model: str | None) -> None:
        """Set override. Kirim None/'' di keduanya untuk kembali ke router otomatis."""
        await self.set(_KEY_PROVIDER, provider)
        await self.set(_KEY_MODEL, model)

    async def get_compaction_mode(self, default: str = "off") -> str:
        """Mode compaction headroom: off (truncation, aman) | local | cloud.

        Nilai tak dikenal → fail-safe ke `default` (tak diam-diam menyalakan LLM call).
        """
        value = await self.get(_KEY_COMPACTION)
        if value in COMPACTION_MODES:
            return value
        return default

    async def set_compaction_mode(self, mode: str | None) -> None:
        """Set mode compaction. None/'' atau nilai tak valid → kembali ke 'off'."""
        if mode not in COMPACTION_MODES:
            mode = None  # set() menghapus baris → get_compaction_mode pakai default 'off'
        await self.set(_KEY_COMPACTION, mode)

    async def get_ui_locale(self) -> str:
        """Bahasa tampilan WEB UI (bukan bahasa respons agent, §1.5). Default English.

        Terpisah total dari locale agent — agent selalu mengikuti bahasa pesan user.
        Nilai tak dikenal → fail-safe ke DEFAULT_LOCALE (bukan exception).
        """
        value = await self.get(_KEY_UI_LOCALE)
        return value if value in LOCALES else DEFAULT_LOCALE

    async def set_ui_locale(self, locale: str | None) -> None:
        """Set bahasa tampilan UI. None/'' atau nilai tak dikenal → kembali ke default English."""
        if locale not in LOCALES:
            locale = None  # set() menghapus baris → get_ui_locale pakai DEFAULT_LOCALE
        await self.set(_KEY_UI_LOCALE, locale)
