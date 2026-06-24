"""Konfigurasi on/off tiap guardrail rail, lewat UI tanpa restart.

Pola sama `RouterConfigStore`: satu key JSON di `app_settings`. Default (belum
di-set) → semua rail AKTIF (keamanan dulu, §1). Dibaca AgentLoop untuk membangun
`GuardrailEngine(enabled=...)`.

DB-bound (hanya DatabaseManager, §1.6). Engine itu sendiri tetap murni stdlib —
config dipisah agar `security/guardrails.py` tak menyentuh DB (extractable).
"""

import json

from infra.database import DatabaseManager
from infra.logging import log
from security.guardrails import BUILTIN_RAILS, DEFAULT_ENABLED

GUARDRAILS_KEY = "guardrails_enabled"


class GuardrailConfigStore:
    """Baca/tulis peta nama_rail→bool. Tanpa config → semua aktif."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_enabled(self) -> dict[str, bool]:
        """Peta aktif rail. Rail tak dikenal di config diabaikan; rail yang hilang
        dari config dianggap AKTIF (fail-safe: keamanan default-on)."""
        enabled = dict(DEFAULT_ENABLED)
        row = await self.db.fetchone(
            "SELECT value FROM app_settings WHERE key=?", (GUARDRAILS_KEY,)
        )
        if not row or not row["value"]:
            return enabled
        try:
            raw = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            log.warning("guardrails_config_corrupt", value=row["value"][:80])
            return enabled
        for name in BUILTIN_RAILS:
            if name in raw:
                enabled[name] = bool(raw[name])
        return enabled

    async def set_enabled(self, mapping: dict[str, bool]) -> dict:
        """Simpan on/off. Hanya rail yang dikenal disimpan. Return ringkasan."""
        clean = {name: bool(mapping[name]) for name in BUILTIN_RAILS if name in mapping}
        await self.db.execute(
            """INSERT INTO app_settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (GUARDRAILS_KEY, json.dumps(clean)),
        )
        return {"saved": clean}

    async def reset(self) -> None:
        """Hapus config → semua rail kembali aktif (default)."""
        await self.db.execute("DELETE FROM app_settings WHERE key=?", (GUARDRAILS_KEY,))
