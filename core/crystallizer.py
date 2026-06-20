import json
from datetime import datetime
from infra.database import DatabaseManager
from infra.logging import log

MIN_TOOL_CALLS = 3
CONFIDENCE_THRESHOLD = 4

# Audit #4: evaluator harus minimal setara generator.
# Solusi Sonnet TIDAK BOLEH dinilai 7B — ini yang membuat inovasi ini valid.
EVALUATOR_FOR: dict[str, tuple[str, str]] = {
    "gemma4:e2b": ("ollama", "gemma4:e4b"),
    "gemma4:e4b": ("ollama", "gemma4:12b"),
    "gemma4:12b": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-sonnet-4-6": ("anthropic", "claude-sonnet-4-6"),
}
DEFAULT_EVALUATOR = ("anthropic", "claude-haiku-4-5-20251001")


class ConfidenceCrystallizer:
    """
    Inovasi 3: agent menilai kualitas solusinya sebelum menyimpan sebagai skill.
    Confidence < 4 atau ada critical_gaps → status draft, bukan active.
    """

    def __init__(self, role: str, llm, db: DatabaseManager):
        self.role = role
        self.llm = llm
        self.db = db

    def should_attempt(self, history: list) -> bool:
        tool_calls = sum(len(t.tool_calls) for t in history if t.tool_calls)
        return tool_calls >= MIN_TOOL_CALLS

    async def crystallize(
        self, task: str, solution: str, history: list, generator_model: str
    ) -> dict:
        # Audit #4: pilih evaluator minimal setara generator
        eval_provider, eval_model = EVALUATOR_FOR.get(generator_model, DEFAULT_EVALUATOR)
        evaluation = await self._self_evaluate(task, solution, eval_provider, eval_model)

        status = (
            "active"
            if (
                evaluation["confidence"] >= CONFIDENCE_THRESHOLD and not evaluation["critical_gaps"]
            )
            else "draft"
        )

        steps = []
        for turn in history:
            for tc in turn.tool_calls or []:
                steps.append(f"- {tc['name']}: {json.dumps(tc['input'])[:80]}")

        skill_name = self._slug(task)
        content = self._format(task, steps, solution, evaluation)

        try:
            await self.db.execute(
                """
                INSERT INTO skills (role, skill_name, trigger_pattern, skill_content,
                                    status, confidence, generator_model, decay_score)
                VALUES (?,?,?,?,?,?,?,1.0)
                """,
                (
                    self.role,
                    skill_name,
                    task[:60],
                    content,
                    status,
                    evaluation["confidence"] / 5.0,
                    generator_model,
                ),
            )
            # Inovasi 3 observability: catat keputusan evaluator agar kasat mata di /skills.
            await self._log_attempt(skill_name, generator_model, eval_model, status, evaluation)
            return {
                "skill_name": skill_name,
                "status": status,
                "evaluator": eval_model,
                **evaluation,
            }
        except Exception as e:
            # Umumnya UNIQUE constraint (skill sudah ada) → anggap duplicate.
            # Log agar error DB lain tidak hilang diam-diam (CLAUDE.md §6).
            log.warning("crystallize_insert_failed", skill_name=skill_name, error=str(e))
            await self._log_attempt(
                skill_name, generator_model, eval_model, "duplicate", evaluation
            )
            return {"skill_name": skill_name, "status": "duplicate"}

    async def refine_on_correction(self, skill_id: int, correction_trace: str) -> dict:
        """I3 — perbaiki skill yang menyesatkan saat dipakai (gated + versioned).

        Dipicu hanya bila sebuah skill ikut dipakai pada turn yang TURN-BERIKUTNYA
        dikoreksi user. Evaluator ≥ generator (pola EVALUATOR_FOR) menulis ulang konten;
        diterapkan HANYA bila improved && confidence ≥ threshold. Konten lama disimpan
        ke skill_versions (revertible). Confidence rendah → konten TIDAK disentuh
        (fail-safe — jangan belajar dari sinyal lemah, biarkan decay bekerja).
        """
        row = await self.db.fetchone(
            "SELECT skill_name, skill_content, generator_model, version FROM skills WHERE id=?",
            (skill_id,),
        )
        if not row:
            return {"skill_id": skill_id, "action": "noop"}
        gen_model = row["generator_model"] or "gemma4:e4b"
        eval_provider, eval_model = EVALUATOR_FOR.get(gen_model, DEFAULT_EVALUATOR)

        prompt = (
            f"Sebuah skill agent ternyata MENYESATKAN saat dipakai (turn-nya dikoreksi user).\n\n"
            f"SKILL SAAT INI:\n{row['skill_content'][:1500]}\n\n"
            f"KOREKSI USER:\n{correction_trace[:500]}\n\n"
            f"Perbaiki skill agar tak mengulang kesalahan. Jawab HANYA JSON valid:\n"
            f'{{"improved": <true/false>, "confidence": <1-5>, '
            f'"new_content": "<konten skill yang diperbaiki>", "reasoning": "<satu kalimat>"}}'
        )
        response = ""
        async for chunk in self.llm.stream_with_fallback(
            eval_provider, eval_model, [{"role": "user", "content": prompt}]
        ):
            if chunk.type == "text":
                response += chunk.text
        ev = self._parse_refine(response)

        if not ev["improved"] or ev["confidence"] < CONFIDENCE_THRESHOLD or not ev["new_content"]:
            return {"skill_id": skill_id, "action": "skipped", "confidence": ev["confidence"]}

        # Simpan versi lama (revertible) lalu terapkan versi baru.
        await self.db.execute(
            """INSERT INTO skill_versions (skill_id, version, skill_content, reason)
               VALUES (?,?,?, 'refine_on_correction')""",
            (skill_id, row["version"], row["skill_content"]),
        )
        await self.db.execute(
            "UPDATE skills SET skill_content=?, version=version+1 WHERE id=?",
            (ev["new_content"], skill_id),
        )
        return {"skill_id": skill_id, "action": "refined", "confidence": ev["confidence"]}

    def _parse_refine(self, raw: str) -> dict:
        try:
            cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            return {
                "improved": bool(data.get("improved", False)),
                "confidence": int(data.get("confidence", 1)),
                "new_content": str(data.get("new_content", "")).strip(),
                "reasoning": str(data.get("reasoning", "")),
            }
        except (json.JSONDecodeError, ValueError):
            return {
                "improved": False,
                "confidence": 1,
                "new_content": "",
                "reasoning": "parse failed",
            }

    async def _log_attempt(
        self, skill_name: str, generator_model: str, evaluator_model: str, status: str, ev: dict
    ) -> None:
        """Catat satu percobaan kristalisasi ke crystallization_log (fail-soft)."""
        try:
            await self.db.execute(
                """INSERT INTO crystallization_log
                   (role, skill_name, generator_model, evaluator_model,
                    confidence, critical_gaps, status, reasoning)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self.role,
                    skill_name,
                    generator_model,
                    evaluator_model,
                    ev.get("confidence"),
                    int(bool(ev.get("critical_gaps"))),
                    status,
                    ev.get("reasoning", ""),
                ),
            )
        except Exception as e:  # noqa: BLE001 — observability tak boleh ganggu turn
            log.warning("crystallization_log_failed", skill_name=skill_name, error=str(e))

    async def _self_evaluate(self, task: str, solution: str, provider: str, model: str) -> dict:
        prompt = (
            f"Nilai kualitas solusi berikut secara objektif.\n\n"
            f"TASK: {task}\n\nSOLUSI:\n{solution[:1500]}\n\n"
            f"Jawab HANYA JSON valid, tanpa teks lain:\n"
            f'{{"confidence": <1-5>, "critical_gaps": <true/false>, "reasoning": "<satu kalimat>"}}'
        )
        response = ""
        async for chunk in self.llm.stream_with_fallback(
            provider, model, [{"role": "user", "content": prompt}]
        ):
            if chunk.type == "text":
                response += chunk.text
        return self._parse(response)

    def _parse(self, raw: str) -> dict:
        try:
            cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(cleaned)
            return {
                "confidence": int(data.get("confidence", 1)),
                "critical_gaps": bool(data.get("critical_gaps", True)),
                "reasoning": str(data.get("reasoning", "")),
            }
        except (json.JSONDecodeError, ValueError):
            # Parse gagal → fail-safe ke confidence rendah agar tidak masuk active
            return {"confidence": 1, "critical_gaps": True, "reasoning": "parse failed"}

    def _format(self, task: str, steps: list[str], solution: str, ev: dict) -> str:
        return (
            f"# Skill: {self._slug(task)}\n\n"
            f"## Trigger\n{task[:200]}\n\n"
            f"## Steps\n{chr(10).join(steps)}\n\n"
            f"## Outcome\n{solution[:400]}\n\n"
            f"## Self-evaluation\n"
            f"- Confidence: {ev['confidence']}/5\n"
            f"- Critical gaps: {ev['critical_gaps']}\n"
            f"- Reasoning: {ev['reasoning']}\n\n"
            f"## Metadata\n"
            f"- Role: {self.role}\n"
            f"- Created: {datetime.now().isoformat()}\n"
        )

    def _slug(self, task: str) -> str:
        words = task.lower().split()[:5]
        return "-".join(w for w in words if w.isalnum()) or "unnamed-skill"
