import time
from datetime import datetime
from infra.database import DatabaseManager
from infra.config import AppConfig


class SkillDecayManager:
    """
    Inovasi 2: skill yang jarang dipakai memudar secara eksponensial dan ter-arsip.
    Decay formula: score = score * (0.97 ^ hari_sejak_dipakai).
    """

    def __init__(self, role: str, db: DatabaseManager, config: AppConfig):
        self.role = role
        self.db = db
        self.config = config
        self._last_decay_ts: float = 0.0

    async def get_active_skills(self, query: str) -> list[dict]:
        return await self.db.fetchall(
            """SELECT id, skill_name, skill_content, trigger_pattern, decay_score
               FROM skills
               WHERE role=? AND status='active'
                 AND (trigger_pattern IS NULL OR ? LIKE '%' || trigger_pattern || '%')
               ORDER BY decay_score DESC, use_count DESC LIMIT ?""",
            (self.role, query, self.config.max_active_skills),
        )

    async def mark_used(self, skill_id: int) -> None:
        """Skill dipakai lagi → revive: status kembali active, score naik."""
        await self.db.execute(
            """UPDATE skills
               SET use_count = use_count + 1, last_used_at = ?,
                   decay_score = MIN(1.0, decay_score + ?),
                   status = CASE WHEN status='archived' THEN 'active' ELSE status END
               WHERE id = ?""",
            (datetime.now().isoformat(), self.config.skill_revive_boost, skill_id),
        )

    async def maybe_run_decay_pass(self) -> dict:
        """
        Audit #7: throttle — hanya jalan jika sudah lewat decay_interval_sec.
        Dipanggil tiap turn, tapi mayoritas no-op.
        """
        now = time.monotonic()
        if now - self._last_decay_ts < self.config.decay_interval_sec:
            return {"skipped": True}
        self._last_decay_ts = now
        return await self._run_decay_pass()

    async def _run_decay_pass(self) -> dict:
        # Audit #6: exponential decay via POWER() — didaftarkan sebagai custom function di DatabaseManager
        await self.db.execute(
            """UPDATE skills
               SET decay_score = decay_score * POWER(?,
                   julianday('now') - julianday(COALESCE(last_used_at, created_at)))
               WHERE role=? AND status='active'""",
            (self.config.skill_decay_base, self.role),
        )
        cursor = await self.db.execute(
            """UPDATE skills SET status='archived'
               WHERE role=? AND status='active' AND decay_score < ?""",
            (self.role, self.config.skill_archive_threshold),
        )
        return {"archived": cursor.rowcount}
