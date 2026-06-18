"""Override peta tier→model router lewat UI (/router), tanpa mengubah kode.

Router tetap memutuskan TIER (complexity) otomatis; store ini hanya menentukan
MODEL+provider untuk tiap tier. Disimpan sebagai satu key JSON di app_settings.
Default kosong → SmartRouter pakai MODELS hardcoded. Dibaca AgentLoop per-turn,
lalu di-set ke router.model_map sebelum decide() (pola sama threshold_offset).

DB-bound (hanya DatabaseManager, §1.6) — extractable. Apply = pilihan sadar user
lewat tombol, bukan auto.
"""

import json

from core.router import Complexity, SmartRouter
from infra.database import DatabaseManager
from infra.logging import log

# Key di app_settings tempat peta override disimpan (JSON).
ROUTER_MODEL_MAP_KEY = "router_model_map"
# Provider yang dikenal — peta yang menyebut provider lain ditolak (fail-safe).
KNOWN_PROVIDERS = {"ollama", "gemini", "anthropic"}


class RouterConfigStore:
    """Baca/tulis override peta tier→model. Tanpa override → default MODELS."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_map(self) -> dict[Complexity, tuple[str, str, float]]:
        """Peta aktif tier→(model, provider, cost). Default MODELS bila belum di-set.

        Korup/parsial → fail-safe ke default penuh agar router tak pernah kehilangan tier.
        """
        default = dict(SmartRouter.MODELS)
        row = await self.db.fetchone(
            "SELECT value FROM app_settings WHERE key=?", (ROUTER_MODEL_MAP_KEY,)
        )
        if not row or not row["value"]:
            return default
        try:
            raw = json.loads(row["value"])  # {"trivial": {"model":..,"provider":..}, ...}
        except (json.JSONDecodeError, TypeError):
            log.warning("router_model_map_corrupt", value=row["value"][:80])
            return default
        out = dict(default)
        for tier in Complexity:
            entry = raw.get(tier.value)
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model", "")).strip()
            provider = str(entry.get("provider", "")).strip()
            if model and provider in KNOWN_PROVIDERS:
                out[tier] = (model, provider, 0.0)  # cost nyata tak dipetakan; jangan tebak
        return out

    async def set_map(self, mapping: dict[str, dict[str, str]]) -> dict:
        """Simpan peta override. `mapping`: {tier_value: {model, provider}}.

        Hanya tier valid & provider dikenal yang disimpan. Return ringkasan tier tersimpan.
        """
        clean: dict[str, dict[str, str]] = {}
        for tier in Complexity:
            entry = mapping.get(tier.value)
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model", "")).strip()
            provider = str(entry.get("provider", "")).strip()
            if model and provider in KNOWN_PROVIDERS:
                clean[tier.value] = {"model": model, "provider": provider}
        await self.db.execute(
            """INSERT INTO app_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (ROUTER_MODEL_MAP_KEY, json.dumps(clean)),
        )
        return {"saved_tiers": list(clean.keys())}

    async def reset(self) -> None:
        """Hapus override → kembali ke peta default MODELS."""
        await self.db.execute("DELETE FROM app_settings WHERE key=?", (ROUTER_MODEL_MAP_KEY,))

    async def is_overridden(self) -> bool:
        row = await self.db.fetchone(
            "SELECT value FROM app_settings WHERE key=?", (ROUTER_MODEL_MAP_KEY,)
        )
        return bool(row and row["value"] and row["value"] != "{}")
