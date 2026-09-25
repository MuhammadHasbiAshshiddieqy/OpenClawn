import re

from infra.database import DatabaseManager
from infra.config import AppConfig

# Audit 2026-09-25: pencocokan trigger skill. SEBELUMNYA `query LIKE
# '%' || trigger || '%'` dengan trigger = 60 char pertama task asli — query baru
# harus memuat kalimat itu PERSIS, sehingga skill praktis tak pernah terpicu
# ulang (Inovasi 2 revive & Inovasi 3 promote jarang terjadi), dan `%`/`_` di
# teks task diperlakukan sebagai wildcard. Sekarang: substring literal ATAU
# tumpang-tindih kata (≥ TRIGGER_OVERLAP dari kata bermakna di trigger).
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MIN_WORD_LEN = 3
TRIGGER_OVERLAP = 0.6
# Kandidat yang dinilai di Python per query (urut decay_score) — batas atas
# agar role dengan ribuan skill tak memuat semuanya tiap turn.
_CANDIDATE_LIMIT = 200


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= _MIN_WORD_LEN}


def trigger_matches(query: str, trigger: str | None) -> bool:
    """True bila skill dengan `trigger` relevan untuk `query`. Trigger kosong =
    selalu relevan (perilaku lama `trigger_pattern IS NULL`)."""
    if not trigger or not trigger.strip():
        return True
    q = query.lower()
    t = trigger.lower().strip()
    if t in q:
        return True
    t_words = _words(t)
    if len(t_words) < 2:
        return False  # satu kata pendek → hanya substring, hindari false positive
    return len(t_words & _words(q)) / len(t_words) >= TRIGGER_OVERLAP


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
        # Multi-Tenant (TODO.md § Prioritas 5) — bukti konsep wiring penuh: semua
        # query skill (baca & decay) di-scope ke tenant ini. Deployment single-tenant
        # existing tetap jalan tanpa perubahan (default 'default').
        self.tenant_id = tenant_id
        # Audit 2026-09-25 (#4): timestamp pass terakhir disimpan di DB, BUKAN
        # atribut instance — AgentLoop (dan manager ini) dibuat BARU tiap request
        # web, jadi throttle per-instance lama tak pernah menahan apa pun: decay
        # jalan TIAP turn. Pola sama curator/calibration (app_settings).
        self._last_pass_key = f"decay_last_pass:{tenant_id}:{role}"

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
        cols = "id, skill_name, skill_content, trigger_pattern, decay_score, status"
        active_rows = await self.db.fetchall(
            f"""SELECT {cols} FROM skills
               WHERE tenant_id=? AND role=? AND status='active'
               ORDER BY decay_score DESC, use_count DESC LIMIT ?""",
            (self.tenant_id, self.role, _CANDIDATE_LIMIT),
        )
        active = [r for r in active_rows if trigger_matches(query, r["trigger_pattern"])]
        active = active[: self.config.max_active_skills]
        # I2: beri 1 slot percobaan untuk draft yang trigger-nya cocok — satu-satunya
        # cara draft bisa terbukti & dipromosikan. Draft trial TIDAK menggusur active.
        draft_rows = await self.db.fetchall(
            f"""SELECT {cols} FROM skills
               WHERE tenant_id=? AND role=? AND status='draft' AND trigger_pattern IS NOT NULL
               ORDER BY draft_success_count DESC, id DESC LIMIT ?""",
            (self.tenant_id, self.role, _CANDIDATE_LIMIT),
        )
        trial = [r for r in draft_rows if trigger_matches(query, r["trigger_pattern"])][:1]
        shared_rows = await self.db.fetchall(
            f"""SELECT {cols} FROM skills
               WHERE tenant_id=? AND role!=? AND status='active'
                 AND visibility IN ('shared','inherited')
               ORDER BY decay_score DESC, use_count DESC LIMIT ?""",
            (self.tenant_id, self.role, _CANDIDATE_LIMIT),
        )
        shared = [r for r in shared_rows if trigger_matches(query, r["trigger_pattern"])]
        shared = shared[: self.config.max_shared_skills]
        return active + trial + shared

    async def mark_used(self, skill_id: int) -> None:
        """Skill dipakai lagi → revive: status kembali active, score naik.

        Isolasi tenant: `tenant_id=?` mencegah turn tenant A me-revive/mempengaruhi
        skill id milik tenant B walau id tertebak (defense-in-depth, sama pola
        ChatSessionStore.soft_delete)."""
        await self.db.execute(
            """UPDATE skills
               SET use_count = use_count + 1, last_used_at = datetime('now'),
                   decay_score = MIN(1.0, decay_score + ?),
                   status = CASE WHEN status='archived' THEN 'active' ELSE status END
               WHERE id = ? AND tenant_id = ?""",
            (self.config.skill_revive_boost, skill_id, self.tenant_id),
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
                       confidence=?, last_used_at=datetime('now') WHERE id=? AND tenant_id=?""",
                (new_count, promoted_conf, skill_id, self.tenant_id),
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

        Audit 2026-09-25 (#4): throttle kini persisten di `app_settings` dan
        diklaim ATOMIK (compare-and-set) — dua turn bersamaan tak bisa sama-sama
        menjalankan pass untuk jendela waktu yang sama.
        """
        now_row = await self.db.fetchone("SELECT julianday('now') AS now")
        now = float(now_row["now"])
        row = await self.db.fetchone(
            "SELECT value FROM app_settings WHERE key=?", (self._last_pass_key,)
        )
        last: float | None = None
        if row and row["value"]:
            try:
                last = float(row["value"])
            except (ValueError, TypeError):
                last = None
        if last is not None and (now - last) * 86400 < self.config.decay_interval_sec:
            return {"skipped": True}

        if row is None:
            claim = await self.db.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (self._last_pass_key, repr(now)),
            )
        else:
            claim = await self.db.execute(
                """UPDATE app_settings SET value=?, updated_at=CURRENT_TIMESTAMP
                   WHERE key=? AND value IS ?""",
                (repr(now), self._last_pass_key, row["value"]),
            )
        if claim.rowcount != 1:
            return {"skipped": True}  # pass lain sudah mengklaim jendela ini
        return await self._run_decay_pass(since=last, now=now)

    async def _run_decay_pass(self, since: float | None = None, now: float | None = None) -> dict:
        """Satu pass decay.

        Audit 2026-09-25 (#4): eksponen = hari sejak MAX(terakhir dipakai, pass
        sebelumnya) — BUKAN hari sejak terakhir dipakai saja. Formula lama
        `score *= 0.97^hari_sejak_dipakai` di setiap pass MENGGANDAKAN decay
        (skill 2 hari tak dipakai kehilangan 0.97^2 LAGI tiap pass → terarsip
        dalam hitungan jam/turn, bukan ~40 hari). Dengan eksponen inkremental,
        total decay kumulatif tetap tepat `0.97^hari_sejak_dipakai` seperti spec.
        `since`/`now` dalam julian day; None → pass pertama (perilaku lama).
        """
        now_expr = "?" if now is not None else "julianday('now')"
        params: tuple = (self.config.skill_decay_base,)
        params += (now,) if now is not None else ()
        params += (since if since is not None else 0.0, self.tenant_id, self.role)
        # Audit #6: exponential decay via POWER() — didaftarkan sebagai custom function di DatabaseManager
        await self.db.execute(
            f"""UPDATE skills
               SET decay_score = decay_score * POWER(?, MAX(0.0, {now_expr} - MAX(
                   julianday(COALESCE(last_used_at, created_at)), ?)))
               WHERE tenant_id=? AND role=? AND status='active'""",
            params,
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
