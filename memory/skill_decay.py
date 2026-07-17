import time
from datetime import datetime
from infra.database import DatabaseManager
from infra.config import AppConfig


class SkillDecayManager:
    """
    Inovasi 2: skill yang jarang dipakai memudar secara eksponensial dan ter-arsip.
    Decay formula: score = score * (0.97 ^ hari_sejak_dipakai).
    """

    def __init__(
        self, role: str, db: DatabaseManager, config: AppConfig, tenant_id: str = "default"
    ):
        self.role = role
        self.db = db
        self.config = config
        self._last_decay_ts: float = 0.0
        # Multi-Tenant (TODO.md § Prioritas 5) — bukti konsep wiring penuh: semua
        # query skill (baca & decay) di-scope ke tenant ini. Deployment single-tenant
        # existing tetap jalan tanpa perubahan (default 'default').
        self.tenant_id = tenant_id

    async def get_active_skills(self, query: str) -> list[dict]:
        """Skill aktif yang trigger-nya cocok query, untuk disuntik ke context.

        Menyertakan `status` agar pemanggil bisa membedakan skill 'active' dari
        'draft' percobaan (I2): draft yang trigger-nya cocok diberi SATU slot
        percobaan agar bisa membuktikan diri & naik kelas. Draft tak menggusur
        active (di-LIMIT terpisah & ditambahkan di belakang).

        Skill Marketplace lintas-role (TODO.md § Prioritas 6): skill role LAIN
        dengan `visibility IN ('shared','inherited')` ikut disertakan (di-LIMIT
        terpisah, di bagian akhir — tak menggusur skill role sendiri yang lebih
        relevan). `private` (default) TETAP hanya terlihat role pemiliknya,
        perilaku lama tak berubah untuk skill yang belum di-share sadar.
        """
        active = await self.db.fetchall(
            """SELECT id, skill_name, skill_content, trigger_pattern, decay_score, status
               FROM skills
               WHERE tenant_id=? AND role=? AND status='active'
                 AND (trigger_pattern IS NULL OR ? LIKE '%' || trigger_pattern || '%')
               ORDER BY decay_score DESC, use_count DESC LIMIT ?""",
            (self.tenant_id, self.role, query, self.config.max_active_skills),
        )
        # I2: beri 1 slot percobaan untuk draft yang trigger-nya cocok — satu-satunya
        # cara draft bisa terbukti & dipromosikan. Draft trial TIDAK menggusur active.
        trial = await self.db.fetchall(
            """SELECT id, skill_name, skill_content, trigger_pattern, decay_score, status
               FROM skills
               WHERE tenant_id=? AND role=? AND status='draft'
                 AND trigger_pattern IS NOT NULL AND ? LIKE '%' || trigger_pattern || '%'
               ORDER BY draft_success_count DESC, id DESC LIMIT 1""",
            (self.tenant_id, self.role, query),
        )
        shared = await self.db.fetchall(
            """SELECT id, skill_name, skill_content, trigger_pattern, decay_score, status
               FROM skills
               WHERE tenant_id=? AND role!=? AND status='active'
                 AND visibility IN ('shared','inherited')
                 AND (trigger_pattern IS NULL OR ? LIKE '%' || trigger_pattern || '%')
               ORDER BY decay_score DESC, use_count DESC LIMIT ?""",
            (self.tenant_id, self.role, query, self.config.max_shared_skills),
        )
        return active + trial + shared

    async def mark_used(self, skill_id: int) -> None:
        """Skill dipakai lagi → revive: status kembali active, score naik.

        Isolasi tenant: `tenant_id=?` mencegah turn tenant A me-revive/mempengaruhi
        skill id milik tenant B walau id tertebak (defense-in-depth, sama pola
        ChatSessionStore.soft_delete)."""
        await self.db.execute(
            """UPDATE skills
               SET use_count = use_count + 1, last_used_at = ?,
                   decay_score = MIN(1.0, decay_score + ?),
                   status = CASE WHEN status='archived' THEN 'active' ELSE status END
               WHERE id = ? AND tenant_id = ?""",
            (datetime.now().isoformat(), self.config.skill_revive_boost, skill_id, self.tenant_id),
        )

    async def mark_many_used(self, skill_ids: list[int]) -> None:
        """Revive beberapa skill sekaligus (skill yang dipakai pada satu turn).

        Prasyarat I2/I3: turn yang memakai skill harus menandainya sebagai terpakai
        agar revive (Inovasi 2) benar-benar terjadi — sebelumnya `mark_used` ada tapi
        tak pernah dipanggil dari agent loop (revive dorman).
        """
        for sid in skill_ids:
            await self.mark_used(sid)

    async def record_draft_outcome(self, skill_id: int, success: bool) -> dict:
        """I2 — draft auto-promotion (tetap gated, bukti berulang).

        success=True  → +1 `draft_success_count`; bila ≥ draft_promote_uses → promote
                        ke 'active' (confidence dinaikkan ke ambang).
        success=False → reset counter (bukti negatif menghapus akumulasi positif).
        Hanya berefek pada skill berstatus 'draft'. Return ringkasan untuk audit.
        """
        row = await self.db.fetchone(
            "SELECT status, draft_success_count, confidence FROM skills WHERE id=? AND tenant_id=?",
            (skill_id, self.tenant_id),
        )
        if not row or row["status"] != "draft":
            return {"skill_id": skill_id, "action": "noop"}

        if not success:
            await self.db.execute(
                "UPDATE skills SET draft_success_count=0 WHERE id=? AND tenant_id=?",
                (skill_id, self.tenant_id),
            )
            return {"skill_id": skill_id, "action": "reset"}

        new_count = (row["draft_success_count"] or 0) + 1
        if new_count >= self.config.draft_promote_uses:
            # Promote: status active + confidence minimal ke ambang (threshold/5).
            promoted_conf = max(row["confidence"] or 0.0, self.config.confidence_threshold / 5.0)
            await self.db.execute(
                """UPDATE skills SET status='active', draft_success_count=?,
                       confidence=?, last_used_at=? WHERE id=? AND tenant_id=?""",
                (new_count, promoted_conf, datetime.now().isoformat(), skill_id, self.tenant_id),
            )
            return {"skill_id": skill_id, "action": "promoted", "uses": new_count}

        await self.db.execute(
            "UPDATE skills SET draft_success_count=? WHERE id=? AND tenant_id=?",
            (new_count, skill_id, self.tenant_id),
        )
        return {"skill_id": skill_id, "action": "incremented", "uses": new_count}

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
               WHERE tenant_id=? AND role=? AND status='active'""",
            (self.config.skill_decay_base, self.tenant_id, self.role),
        )
        cursor = await self.db.execute(
            """UPDATE skills SET status='archived'
               WHERE tenant_id=? AND role=? AND status='active' AND decay_score < ?""",
            (self.tenant_id, self.role, self.config.skill_archive_threshold),
        )
        archived = cursor.rowcount

        # Draft cleanup: draft TUA yang tak pernah terbukti (draft_success_count=0)
        # diarsipkan agar tak menumpuk. ARSIP, bukan hapus (tak ada kehilangan data
        # senyap — bisa ditinjau di /skills). draft_stale_days=0 → fitur nonaktif.
        drafts_archived = 0
        if self.config.draft_stale_days > 0:
            cur2 = await self.db.execute(
                """UPDATE skills SET status='archived'
                   WHERE tenant_id=? AND role=? AND status='draft' AND draft_success_count=0
                     AND julianday('now') - julianday(created_at) > ?""",
                (self.tenant_id, self.role, self.config.draft_stale_days),
            )
            drafts_archived = cur2.rowcount
        return {"archived": archived, "drafts_archived": drafts_archived}
