"""Dialectic user model (I5, opsional) — profil naratif user lintas sesi.

L2 menyimpan fakta secara flat; ini merangkumnya jadi profil naratif singkat yang
disuntik sebagai blok stabil di awal context (cocok prompt-caching). Throttled
(user_model_interval_sec), versioned + revertible (tak ada drift senyap). Opsional
(user_model_enabled) & lokal — dapat dihapus user (privasi §1).

Extractable: DatabaseManager + llm (disuntik). Default OFF — tak berefek bila tak diaktifkan.
"""

import time

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.logging import log

PROFILE_MAX_CHARS = 600


class UserModel:
    """Bangun & sajikan profil user naratif per role. Default nonaktif."""

    def __init__(self, role: str, db: DatabaseManager, llm, config: AppConfig):
        self.role = role
        self.db = db
        self.llm = llm
        self.config = config
        self._ts_key = f"user_model_last_ts:{role}"

    async def get_active_profile(self) -> str:
        """Profil aktif untuk disuntik ke context (string kosong bila tak ada/nonaktif)."""
        if not self.config.user_model_enabled:
            return ""
        row = await self.db.fetchone(
            "SELECT profile FROM user_model WHERE role=? AND active=1 ORDER BY version DESC LIMIT 1",
            (self.role,),
        )
        return row["profile"] if row else ""

    async def maybe_update(self) -> dict:
        """Throttled: rangkum L2 facts → profil naratif baru (versioned). Opt-in."""
        if not self.config.user_model_enabled:
            return {"skipped": True, "reason": "disabled"}
        row = await self.db.fetchone("SELECT value FROM app_settings WHERE key=?", (self._ts_key,))
        now = time.time()
        if row and row["value"]:
            try:
                if now - float(row["value"]) < self.config.user_model_interval_sec:
                    return {"skipped": True, "reason": "throttled"}
            except (ValueError, TypeError):
                pass
        await self.db.execute(
            """INSERT INTO app_settings (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (self._ts_key, str(now)),
        )

        facts = await self.db.fetchall(
            "SELECT fact FROM memory_l2 WHERE role=? ORDER BY importance DESC LIMIT 30",
            (self.role,),
        )
        if not facts:
            return {"skipped": True, "reason": "no_facts"}

        joined = "\n".join(f"- {f['fact']}" for f in facts)
        prompt = (
            "Rangkum apa yang diketahui tentang user ini menjadi profil naratif SINGKAT "
            "(maks 3 kalimat) untuk membantu agent menyesuaikan diri. Hanya fakta, tanpa tebakan.\n\n"
            f"FAKTA:\n{joined}\n\nProfil:"
        )
        profile = ""
        try:
            async for chunk in self.llm.stream_with_fallback(
                "ollama", "gemma4:e4b", [{"role": "user", "content": prompt}]
            ):
                if chunk.type == "text":
                    profile += chunk.text
        except Exception as e:  # noqa: BLE001 — profil opsional, gagal jangan ganggu
            log.warning("user_model_update_failed", role=self.role, error=str(e))
            return {"skipped": True, "reason": "llm_failed"}

        profile = profile.strip()[:PROFILE_MAX_CHARS]
        if not profile:
            return {"skipped": True, "reason": "empty"}

        # Versioned: nonaktifkan versi lama, simpan versi baru aktif.
        last = await self.db.fetchone(
            "SELECT MAX(version) AS v FROM user_model WHERE role=?", (self.role,)
        )
        next_ver = (last["v"] or 0) + 1
        await self.db.execute("UPDATE user_model SET active=0 WHERE role=?", (self.role,))
        await self.db.execute(
            "INSERT INTO user_model (role, version, profile, active) VALUES (?,?,?,1)",
            (self.role, next_ver, profile),
        )
        return {"skipped": False, "version": next_ver}

    async def clear(self) -> None:
        """Hapus profil user (privasi §1 — user dapat menghapus kapan saja)."""
        await self.db.execute("DELETE FROM user_model WHERE role=?", (self.role,))
