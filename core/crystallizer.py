import json
from datetime import datetime
from infra.database import DatabaseManager
from infra.logging import log

MIN_TOOL_CALLS = 3
CONFIDENCE_THRESHOLD = 4

# Audit #4: evaluator harus minimal setara generator.
# Solusi Sonnet TIDAK BOLEH dinilai 7B — ini yang membuat inovasi ini valid.
# Peta ini HARUS disinkronkan manual tiap kali router.SmartRouter.MODELS berubah —
# tak ada cara otomatis membandingkan "kekuatan" model lintas provider. Kalau
# generator_model tak dikenal di sini (roster router berubah, atau /router
# override tier ke model baru), _resolve_evaluator() menandai unverified alih-alih
# diam-diam memakai DEFAULT_EVALUATOR seolah pasti aman.
EVALUATOR_FOR: dict[str, tuple[str, str]] = {
    "gemma4:e2b": ("ollama", "gemma4:e4b"),
    "gemma4:e4b": ("ollama", "gemma4:12b"),
    "gemma4:12b": ("anthropic", "claude-haiku-4-5-20251001"),
    "deepseek-r1:latest": ("anthropic", "claude-haiku-4-5-20251001"),
    "qwen3.5:9b": ("anthropic", "claude-haiku-4-5-20251001"),
    "gemini-2.5-flash": ("gemini", "gemini-2.5-pro"),
    "gemini-2.5-pro": ("anthropic", "claude-sonnet-4-6"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-sonnet-4-6": ("anthropic", "claude-sonnet-4-6"),
}
DEFAULT_EVALUATOR = ("anthropic", "claude-haiku-4-5-20251001")


class ConfidenceCrystallizer:
    """
    Inovasi 3: agent menilai kualitas solusinya sebelum menyimpan sebagai skill.
    Confidence < 4 atau ada critical_gaps → status draft, bukan active.
    """

    def __init__(self, role: str, llm, db: DatabaseManager, tenant_id: str = "default"):
        self.role = role
        self.llm = llm
        self.db = db
        # Audit 2026-09-25: skill hasil kristalisasi SEBELUMNYA selalu masuk tenant
        # default (INSERT tanpa tenant_id) dan refine mengubah skill by-id tanpa
        # filter tenant — tak konsisten dengan SkillDecayManager yang sudah
        # di-scope penuh per tenant (Prioritas 5).
        self.tenant_id = tenant_id

    def should_attempt(self, history: list) -> bool:
        tool_calls = sum(len(t.tool_calls) for t in history if t.tool_calls)
        return tool_calls >= MIN_TOOL_CALLS

    async def crystallize(
        self, task: str, solution: str, history: list, generator_model: str
    ) -> dict:
        # Audit #4: pilih evaluator minimal setara generator
        eval_provider, eval_model, verified = self._resolve_evaluator(generator_model)
        evaluation = await self._self_evaluate(task, solution, eval_provider, eval_model)
        # Audit 2026-09-25 (#5): evaluator yang JATUH ke fallback chain (mis.
        # gemini-2.5-pro gagal → gemma4:e4b) bukan lagi evaluator yang dijamin
        # setara generator — hasilnya tak boleh membuat skill 'active'.
        fallback_model = evaluation.pop("_fallback_model", None)
        if fallback_model:
            log.warning(
                "crystallizer_evaluator_fell_back",
                generator_model=generator_model,
                intended=eval_model,
                actual=fallback_model,
            )
            verified = False
            eval_model = fallback_model

        status = (
            "active"
            if (
                verified
                and evaluation["confidence"] >= CONFIDENCE_THRESHOLD
                and not evaluation["critical_gaps"]
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
                INSERT INTO skills (tenant_id, role, skill_name, trigger_pattern, skill_content,
                                    status, confidence, generator_model, decay_score)
                VALUES (?,?,?,?,?,?,?,?,1.0)
                """,
                (
                    self.tenant_id,
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
            "SELECT skill_name, skill_content, generator_model, version FROM skills "
            "WHERE id=? AND tenant_id=?",
            (skill_id, self.tenant_id),
        )
        if not row:
            return {"skill_id": skill_id, "action": "noop"}
        gen_model = row["generator_model"] or "gemma4:e4b"
        eval_provider, eval_model, verified = self._resolve_evaluator(gen_model)
        if not verified:
            # Fail-safe (sama pola dgn confidence rendah): jangan tulis ulang skill
            # pakai evaluator yang kekuatannya relatif terhadap generator tak diketahui.
            return {"skill_id": skill_id, "action": "skipped", "confidence": 0}

        prompt = (
            f"Sebuah skill agent ternyata MENYESATKAN saat dipakai (turn-nya dikoreksi user).\n\n"
            f"SKILL SAAT INI:\n{row['skill_content'][:1500]}\n\n"
            f"KOREKSI USER:\n{correction_trace[:500]}\n\n"
            f"Perbaiki skill agar tak mengulang kesalahan. Jawab HANYA JSON valid:\n"
            f'{{"improved": <true/false>, "confidence": <1-5>, '
            f'"new_content": "<konten skill yang diperbaiki>", "reasoning": "<satu kalimat>"}}'
        )
        response, fallback_model = await self._ask_evaluator(eval_provider, eval_model, prompt)
        if fallback_model:
            # Audit 2026-09-25 (#5): sama alasan crystallize — jangan tulis ulang
            # skill pakai evaluator pengganti yang kekuatannya tak terjamin.
            return {"skill_id": skill_id, "action": "skipped", "confidence": 0}
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
            "UPDATE skills SET skill_content=?, version=version+1 WHERE id=? AND tenant_id=?",
            (ev["new_content"], skill_id, self.tenant_id),
        )
        return {"skill_id": skill_id, "action": "refined", "confidence": ev["confidence"]}

    def _resolve_evaluator(self, generator_model: str) -> tuple[str, str, bool]:
        """Audit #4: cari evaluator minimal setara generator. Model di luar
        EVALUATOR_FOR berarti kekuatan relatifnya TAK diketahui (roster router
        berubah, atau override /router pilih model baru) — fail-safe ke
        DEFAULT_EVALUATOR tapi tandai unverified, supaya caller tak pernah
        menandai hasil 'active'/'refined' dari evaluator yang tak terjamin."""
        if generator_model in EVALUATOR_FOR:
            provider, model = EVALUATOR_FOR[generator_model]
            return provider, model, True
        log.warning("crystallizer_unverified_evaluator", generator_model=generator_model)
        provider, model = DEFAULT_EVALUATOR
        return provider, model, False

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
        response, fallback_model = await self._ask_evaluator(provider, model, prompt)
        result = self._parse(response)
        if fallback_model:
            result["_fallback_model"] = fallback_model
        return result

    async def _ask_evaluator(self, provider: str, model: str, prompt: str) -> tuple[str, str]:
        """Panggil evaluator; kembalikan (teks, model_fallback_atau_""). Chunk
        `fallback` dari stream_with_fallback menandai evaluator yang DIMINTA tak
        dipakai — caller wajib memperlakukan hasilnya sebagai tak terverifikasi."""
        response = ""
        fallback_model = ""
        async for chunk in self.llm.stream_with_fallback(
            provider, model, [{"role": "user", "content": prompt}]
        ):
            if chunk.type == "fallback" and chunk.fallback_used:
                fallback_model = chunk.fallback_model or "unknown"
            elif chunk.type == "text":
                response += chunk.text
        return response, fallback_model

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
