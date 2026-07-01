"""Skill Curator (I1) — gabungkan/dedup skill mirip agar library tak terfragmentasi.

Compounding intelligence: decay hanya mengarsip skill NGANGGUR; curator menangani
skill AKTIF yang tumpang tindih (mis. tiga varian "parse JSON aman" memboroskan slot
context). Dua tahap: pre-filter leksikal MURAH, lalu LLM judge CERMAT & gated.

Anti kehilangan data (§1): loser TIDAK dihapus — `status='merged'`, `merged_into=winner`,
konten disimpan ke `skill_versions`. Semua revertible. Tiap merge tercatat di `curation_log`.

Mirror pola SkillDecayManager: throttled, post-turn, extractable (DatabaseManager + llm).
"""

import json
import re

from infra.config import AppConfig
from infra.database import DatabaseManager
from infra.logging import log

# Tokenizer sederhana untuk similarity leksikal (tanpa dependency baru — bukan FTS5,
# yang di repo ini hanya ada untuk memory_l4, bukan tabel skills).
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: str, b: str) -> float:
    """Kemiripan Jaccard token (0..1). Deterministik & murah — pre-filter sebelum LLM."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


class SkillCuratorManager:
    """Konsolidasi skill mirip per role. Throttled (curation_interval_sec), gated (judge ≥ N)."""

    def __init__(self, role: str, db: DatabaseManager, llm, config: AppConfig):
        self.role = role
        self.db = db
        self.llm = llm
        self.config = config
        self._last_ts_key = f"curation_last_ts:{role}"

    async def maybe_run_curation_pass(self) -> dict:
        """Throttle via curation_interval_sec (pola sama decay). Dipanggil post-turn."""
        import time

        row = await self.db.fetchone(
            "SELECT value FROM app_settings WHERE key=?", (self._last_ts_key,)
        )
        now = time.time()
        if row and row["value"]:
            try:
                if now - float(row["value"]) < self.config.curation_interval_sec:
                    return {"skipped": True}
            except (ValueError, TypeError):
                pass
        await self.db.execute(
            """INSERT INTO app_settings (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (self._last_ts_key, str(now)),
        )
        return await self._run_pass()

    async def _run_pass(self) -> dict:
        """Jalankan satu pass kurasi.

        `curation_auto=False` (default, §8): merge yang disetujui judge HANYA
        diusulkan (curation_log.status='pending') — ditunggu klik apply manusia
        di /skills. `curation_auto=True`: merge langsung diterapkan (tetap revertible).
        """
        pairs = await self._find_candidate_pairs()
        merged = 0
        proposed = 0
        for id_a, id_b, sim in pairs[: self.config.curation_max_pairs_per_pass]:
            a = await self.db.fetchone("SELECT * FROM skills WHERE id=?", (id_a,))
            b = await self.db.fetchone("SELECT * FROM skills WHERE id=?", (id_b,))
            if not a or not b or a["status"] != "active" or b["status"] != "active":
                continue
            judge = await self._judge(a, b)
            if (
                judge["should_merge"]
                and judge["confidence"] >= self.config.curation_judge_min_confidence
            ):
                if self.config.curation_auto:
                    await self._merge(a, b, sim, judge)
                    merged += 1
                else:
                    await self._propose(a, b, sim, judge)
                    proposed += 1
        return {
            "skipped": False,
            "candidates": len(pairs),
            "merged": merged,
            "proposed": proposed,
        }

    async def _find_candidate_pairs(self) -> list[tuple[int, int, float]]:
        """Pre-filter leksikal: pasangan skill active dengan Jaccard ≥ threshold.

        O(n²) per role — aman karena n dibatasi (max_active_skills kecil & curation jarang).
        """
        skills = await self.db.fetchall(
            """SELECT id, skill_name, trigger_pattern, skill_content
               FROM skills WHERE role=? AND status='active' ORDER BY id""",
            (self.role,),
        )
        pairs: list[tuple[int, int, float]] = []
        for i in range(len(skills)):
            for j in range(i + 1, len(skills)):
                a, b = skills[i], skills[j]
                text_a = f"{a['skill_name']} {a['trigger_pattern'] or ''} {a['skill_content']}"
                text_b = f"{b['skill_name']} {b['trigger_pattern'] or ''} {b['skill_content']}"
                sim = _jaccard(text_a, text_b)
                if sim >= self.config.curation_similarity_threshold:
                    pairs.append((a["id"], b["id"], sim))
        pairs.sort(key=lambda p: p[2], reverse=True)
        return pairs

    async def _judge(self, a: dict, b: dict) -> dict:
        """LLM judge tier-ringan → keputusan merge terstruktur. Parse gagal → jangan merge."""
        prompt = (
            "Dua skill agent mungkin duplikat. Putuskan apakah sebaiknya digabung jadi satu.\n\n"
            f"SKILL A ({a['skill_name']}):\n{(a['skill_content'] or '')[:800]}\n\n"
            f"SKILL B ({b['skill_name']}):\n{(b['skill_content'] or '')[:800]}\n\n"
            "Jawab HANYA JSON valid:\n"
            '{"should_merge": <true/false>, "confidence": <1-5>, "merged_name": "...", '
            '"merged_content": "<gabungan terbaik>", "reasoning": "<satu kalimat>"}'
        )
        response = ""
        try:
            async for chunk in self.llm.stream_with_fallback(
                "ollama", "gemma4:e4b", [{"role": "user", "content": prompt}]
            ):
                if chunk.type == "text":
                    response += chunk.text
        except Exception as e:  # noqa: BLE001 — judge gagal → jangan merge (fail-safe)
            log.warning("curation_judge_failed", error=str(e))
            return {"should_merge": False, "confidence": 1}
        return self._parse_judge(response)

    def _parse_judge(self, raw: str) -> dict:
        try:
            cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            return {
                "should_merge": bool(data.get("should_merge", False)),
                "confidence": int(data.get("confidence", 1)),
                "merged_name": str(data.get("merged_name", "")).strip(),
                "merged_content": str(data.get("merged_content", "")).strip(),
                "reasoning": str(data.get("reasoning", "")),
            }
        except (json.JSONDecodeError, ValueError):
            return {"should_merge": False, "confidence": 1, "merged_content": ""}

    def _pick_winner(self, a: dict, b: dict) -> tuple[dict, dict]:
        """Winner = skill dengan decay_score tertinggi (lebih relevan); loser yang lain."""
        return (a, b) if (a["decay_score"] or 0) >= (b["decay_score"] or 0) else (b, a)

    async def _merge(self, a: dict, b: dict, similarity: float, judge: dict) -> None:
        """Terapkan merge langsung (curation_auto=True): winner menyerap, loser 'merged'."""
        winner, loser = self._pick_winner(a, b)
        merged_content = judge.get("merged_content") or winner["skill_content"]
        await self._apply_merge(a, b, winner, loser, merged_content, similarity, judge)

    async def _propose(self, a: dict, b: dict, similarity: float, judge: dict) -> None:
        """Usulkan merge (curation_auto=False, default): catat pending, JANGAN ubah skill.

        Skill tetap 'active' sampai manusia meng-apply lewat /skills (§8).
        """
        winner, loser = self._pick_winner(a, b)
        merged_content = judge.get("merged_content") or winner["skill_content"]
        await self.db.execute(
            """INSERT INTO curation_log (role, action, status, winner_id, loser_ids,
                   similarity, judge_confidence, merged_content, reasoning)
               VALUES (?, 'merge', 'pending', ?, ?, ?, ?, ?, ?)""",
            (
                self.role,
                winner["id"],
                json.dumps([loser["id"]]),
                similarity,
                judge["confidence"],
                merged_content,
                judge.get("reasoning", ""),
            ),
        )
        log.info("skill_merge_proposed", role=self.role, winner=winner["id"], loser=loser["id"])

    async def _apply_merge(
        self,
        a: dict,
        b: dict,
        winner: dict,
        loser: dict,
        merged_content: str,
        similarity: float,
        judge: dict,
        *,
        log_status: str = "applied",
    ) -> None:
        """Tulis efek merge ke skills + curation_log. Dipakai oleh `_merge` & apply-pending."""
        # Simpan konten winner LAMA ke versi (revertible) sebelum diganti hasil sintesis.
        await self.db.execute(
            """INSERT INTO skill_versions (skill_id, version, skill_content, reason)
               VALUES (?,?,?, 'merge')""",
            (winner["id"], winner["version"], winner["skill_content"]),
        )
        # Winner mewarisi metrik terbaik dari keduanya + konten sintesis.
        await self.db.execute(
            """UPDATE skills SET
                   skill_content=?,
                   decay_score=MAX(?, ?),
                   use_count=?,
                   confidence=MAX(?, ?),
                   version=version+1
               WHERE id=?""",
            (
                merged_content,
                a["decay_score"] or 0,
                b["decay_score"] or 0,
                (a["use_count"] or 0) + (b["use_count"] or 0),
                a["confidence"] or 0,
                b["confidence"] or 0,
                winner["id"],
            ),
        )
        # Loser TIDAK dihapus: ditandai merged + tunjuk winner (dapat dipulihkan).
        await self.db.execute(
            "UPDATE skills SET status='merged', merged_into=? WHERE id=?",
            (winner["id"], loser["id"]),
        )
        await self.db.execute(
            """INSERT INTO curation_log (role, action, status, winner_id, loser_ids, similarity,
                   judge_confidence, reasoning)
               VALUES (?, 'merge', ?, ?, ?, ?, ?, ?)""",
            (
                self.role,
                log_status,
                winner["id"],
                json.dumps([loser["id"]]),
                similarity,
                judge["confidence"],
                judge.get("reasoning", ""),
            ),
        )
        log.info("skill_merged", role=self.role, winner=winner["id"], loser=loser["id"])

    async def apply_pending_merge(self, curation_log_id: int) -> dict:
        """Terapkan satu usulan merge pending (tombol Apply di /skills, §8).

        Mengubah baris pending yang sama menjadi 'applied' agar riwayat tetap satu jejak
        per keputusan (bukan menduplikasi baris curation_log).
        """
        row = await self.db.fetchone(
            """SELECT * FROM curation_log
               WHERE id=? AND role=? AND action='merge' AND status='pending'""",
            (curation_log_id, self.role),
        )
        if not row:
            return {"applied": False, "reason": "usulan tidak ditemukan atau sudah diproses"}
        try:
            loser_ids = json.loads(row["loser_ids"])
        except (json.JSONDecodeError, TypeError):
            loser_ids = []
        if len(loser_ids) != 1:
            return {"applied": False, "reason": "format usulan tidak valid"}

        winner = await self.db.fetchone("SELECT * FROM skills WHERE id=?", (row["winner_id"],))
        loser = await self.db.fetchone("SELECT * FROM skills WHERE id=?", (loser_ids[0],))
        if not winner or not loser or winner["status"] != "active" or loser["status"] != "active":
            await self.db.execute(
                "UPDATE curation_log SET status='reverted' WHERE id=?", (curation_log_id,)
            )
            return {"applied": False, "reason": "skill sudah berubah sejak diusulkan"}

        merged_content = row["merged_content"] or winner["skill_content"]
        await self._apply_merge(
            winner,
            loser,
            winner,
            loser,
            merged_content,
            row["similarity"] or 0.0,
            {"confidence": row["judge_confidence"], "reasoning": row["reasoning"] or ""},
        )
        # Baris pending asli ditandai selesai (jejak baru sudah ditulis oleh _apply_merge).
        await self.db.execute(
            "UPDATE curation_log SET status='applied' WHERE id=?", (curation_log_id,)
        )
        return {"applied": True, "winner_id": winner["id"], "loser_id": loser["id"]}

    async def revert_last_merge(self) -> dict:
        """Pulihkan merge terakhir yang SUDAH diterapkan: loser → active, winner version-1.

        Untuk tombol di /skills. Mengembalikan ringkasan; no-op bila tak ada merge diterapkan.
        Usulan `pending` tidak termasuk (belum mengubah skill apa pun, tak perlu revert).
        """
        last = await self.db.fetchone(
            """SELECT id, winner_id, loser_ids FROM curation_log
               WHERE role=? AND action='merge' AND status='applied' ORDER BY id DESC LIMIT 1""",
            (self.role,),
        )
        if not last:
            return {"reverted": False, "reason": "tidak ada merge untuk di-revert"}
        try:
            loser_ids = json.loads(last["loser_ids"])
        except (json.JSONDecodeError, TypeError):
            loser_ids = []

        # Pulihkan loser ke active.
        for lid in loser_ids:
            await self.db.execute(
                "UPDATE skills SET status='active', merged_into=NULL WHERE id=?", (lid,)
            )
        # Kembalikan konten winner ke versi sebelum merge (bila tersimpan).
        ver = await self.db.fetchone(
            """SELECT skill_content, version FROM skill_versions
               WHERE skill_id=? AND reason='merge' ORDER BY version DESC LIMIT 1""",
            (last["winner_id"],),
        )
        if ver:
            await self.db.execute(
                "UPDATE skills SET skill_content=?, version=? WHERE id=?",
                (ver["skill_content"], ver["version"], last["winner_id"]),
            )
        # Tandai baris merge asli selesai-di-revert agar tidak ditemukan lagi oleh query ini.
        await self.db.execute("UPDATE curation_log SET status='reverted' WHERE id=?", (last["id"],))
        await self.db.execute(
            """INSERT INTO curation_log (role, action, winner_id, loser_ids, reasoning)
               VALUES (?, 'revert_merge', ?, ?, 'revert merge sebelumnya')""",
            (self.role, last["winner_id"], last["loser_ids"]),
        )
        return {"reverted": True, "winner_id": last["winner_id"], "restored": loser_ids}
