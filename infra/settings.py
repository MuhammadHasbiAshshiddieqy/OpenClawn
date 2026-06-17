"""Runtime settings yang bisa diubah lewat /settings tanpa restart.

Saat ini: override model — memaksa SEMUA routing ke satu (provider, model),
melewati keputusan otomatis SmartRouter. Override adalah *pilihan sadar* untuk
eksperimen/development; router cerdas tetap default jika override kosong.

Murni di atas DatabaseManager (CLAUDE.md §1.6) — tabel app_settings key-value.
"""

from infra.database import DatabaseManager

# Pilihan model yang diketahui, untuk dropdown /settings.
# Bukan pembatas keras — hanya saran tampilan. (provider, model, label).
KNOWN_MODELS: list[tuple[str, str, str]] = [
    ("ollama", "gemma4:e2b", "Gemma4 e2b (lokal, ringan)"),
    ("ollama", "gemma4:e4b", "Gemma4 e4b (lokal)"),
    ("ollama", "gemma4:12b", "Gemma4 12b (lokal, berat)"),
    ("anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5 (cloud)"),
    ("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6 (cloud)"),
    ("gemini", "gemini-2.0-flash", "Gemini 2.0 Flash (cloud)"),
    ("gemini", "gemini-2.5-flash", "Gemini 2.5 Flash (cloud)"),
    ("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro (cloud)"),
]

_KEY_PROVIDER = "model_override_provider"
_KEY_MODEL = "model_override_model"


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
