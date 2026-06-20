"""Skill feedback loop (I2 + I3) — jembatan outcome antar-turn.

Masalah: AgentLoop dibuat baru tiap request, jadi "skill apa yang dipakai turn lalu"
+ "apakah turn itu ternyata salah" terpisah dua turn. Modul ini menjembataninya lewat
tabel `skill_usage_pending`:

  - Post-turn  → `record_usage(skill_ids)`  : simpan skill yang disuntik ke turn ini.
  - Turn-N+1   → `resolve_previous(corrected): proses outcome turn LALU:
        sukses (tak dikoreksi) → revive active + promote draft yang terbukti (I2)
        dikoreksi              → reset draft + refine skill aktif (I3, gated)

Extractable: bergantung DatabaseManager + SkillDecayManager + ConfidenceCrystallizer
(disuntik), tanpa import web/agent_loop.
"""

import json

from core.crystallizer import ConfidenceCrystallizer
from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.logging import log
from memory.skill_decay import SkillDecayManager


class SkillFeedback:
    """Resolusi outcome skill antar-turn — menggerakkan revive (I2) & refine (I3)."""

    def __init__(
        self,
        role: str,
        db: DatabaseManager,
        decay: SkillDecayManager,
        crystallizer: ConfidenceCrystallizer,
        config: AppConfig,
    ):
        self.role = role
        self.db = db
        self.decay = decay
        self.crystallizer = crystallizer
        self.config = config

    async def record_usage(self, session_id: str, skill_ids: list[int]) -> None:
        """Catat skill yang dipakai turn ini (untuk diproses outcome-nya di turn berikutnya)."""
        if not skill_ids:
            return
        try:
            await self.db.execute(
                """INSERT INTO skill_usage_pending (session_id, role, skill_ids)
                   VALUES (?,?,?)""",
                (session_id, self.role, json.dumps(skill_ids)),
            )
        except Exception as e:  # noqa: BLE001 — feedback bukan jalur kritis
            log.warning("skill_usage_record_failed", session=session_id, error=str(e))

    async def resolve_previous(
        self, session_id: str, corrected: bool, correction_trace: str = ""
    ) -> dict:
        """Proses outcome turn SEBELUMNYA yang belum diresolusi (paling baru).

        corrected=False → turn lalu sukses: revive active + promote draft (I2).
        corrected=True  → turn lalu dikoreksi: reset draft + refine aktif (I3, gated).
        Hanya memproses baris pending terbaru yang belum resolved untuk session ini.
        """
        row = await self.db.fetchone(
            """SELECT id, skill_ids FROM skill_usage_pending
               WHERE session_id=? AND resolved=0 ORDER BY id DESC LIMIT 1""",
            (session_id,),
        )
        if not row:
            return {"resolved": 0}
        try:
            skill_ids = json.loads(row["skill_ids"])
        except (json.JSONDecodeError, TypeError):
            skill_ids = []

        summary = {"resolved": row["id"], "corrected": corrected, "promoted": 0, "refined": 0}
        refined = 0
        for sid in skill_ids:
            meta = await self.db.fetchone("SELECT status FROM skills WHERE id=?", (sid,))
            if not meta:
                continue
            status = meta["status"]
            if not corrected:
                # Sukses: skill active di-revive; draft membuktikan diri (I2).
                if status == "active":
                    await self.decay.mark_used(sid)
                elif status == "draft":
                    res = await self.decay.record_draft_outcome(sid, success=True)
                    if res.get("action") == "promoted":
                        summary["promoted"] += 1
            else:
                # Dikoreksi: draft yang ikut → reset; active yang ikut → refine (I3 gated).
                if status == "draft":
                    await self.decay.record_draft_outcome(sid, success=False)
                elif status == "active" and self.config.refine_on_correction:
                    if refined < self.config.refine_max_per_pass:
                        res = await self.crystallizer.refine_on_correction(sid, correction_trace)
                        if res.get("action") == "refined":
                            summary["refined"] += 1
                        refined += 1

        await self.db.execute("UPDATE skill_usage_pending SET resolved=1 WHERE id=?", (row["id"],))
        return summary
