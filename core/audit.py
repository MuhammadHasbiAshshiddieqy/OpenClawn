import json

from infra.database import DatabaseManager
from core.audit_chain import (
    ENTRY_ROUTING_DECISION,
    ENTRY_ROUTING_FINALIZED,
    AuditChain,
)
from core.cost_pricing import PRICING_VERIFIED_ON, estimate_cost_usd
from core.router import Complexity, RouteDecision, SmartRouter

CORRECTION_SIGNALS = [
    # Indonesia
    "salah",
    "bukan itu",
    "coba lagi",
    "maksudku",
    "kurang tepat",
    "tidak benar",
    "ulangi",
    "keliru",
    "bukan begitu",
    "harusnya",
    # English (core harus locale-neutral, §1.5)
    "that's wrong",
    "thats wrong",
    "not what i",
    "try again",
    "incorrect",
    "i meant",
    "no, ",
    "redo",
    "not right",
    "should be",
]


class RoutingAuditor:
    """
    Inovasi 1: catat setiap keputusan routing + apakah terbukti tepat.
    log_decision dipanggil SEBELUM LLM call, finalize SESUDAH.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        # Tamper-evident audit trail (§ Prioritas 9.1). Fail-soft di dalam
        # AuditChain.append — kegagalan rantai tak pernah menjatuhkan turn.
        self.chain = AuditChain(db)

    async def log_decision(
        self,
        session_id: str,
        role: str,
        query: str,
        route: RouteDecision,
        user_id: str = "default",
    ) -> int:
        """`user_id` (§ Audit log format actor_is_agent, TODO.md Prioritas 2):
        AgentConfig.user_id — default 'default' selaras single-user design saat
        ini (CLAUDE.md §7). `actor_is_agent` tidak diparameterkan — SELALU 1 di
        tabel ini (setiap baris routing_events adalah tindakan agent), memakai
        DEFAULT kolom (lihat migrations/001_initial.sql) alih-alih dikirim tiap
        panggilan."""
        d = route.dimensions
        cursor = await self.db.execute(
            """
            INSERT INTO routing_events (
                session_id, role, user_id, query_text,
                dim_query_tokens, dim_has_tech_kw, dim_needs_multistep,
                dim_history_len, dim_role, dim_has_urgency,
                dim_needs_stream, dim_is_continuation, dim_soul_upgrade_hit,
                dim_has_code_signal, dim_query_script, dim_language_bumped,
                complexity_score, complexity_label,
                model_chosen, provider, routing_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                role,
                user_id,
                query,
                d["query_tokens"],
                d["has_tech_kw"],
                d["needs_multistep"],
                d["history_len"],
                d["role"],
                d["has_urgency"],
                d["needs_stream"],
                d["is_continuation"],
                d["soul_upgrade_hit"],
                # Audit produksi 2026-07-27: 3 dimensi multilingual routing di
                # router.py sebelumnya di-drop diam-diam di sini (lihat _ADDED_COLUMNS).
                d.get("has_code_signal"),
                d.get("query_script"),
                d.get("language_bumped"),
                route.complexity_score,
                route.complexity.value,
                route.model,
                route.provider,
                route.reason,
            ),
        )
        event_id = cursor.lastrowid
        # Rantai audit: catat KEPUTUSAN sebelum LLM dipanggil. Payload sengaja
        # ringkas (bukan salinan penuh baris) — ref_id menunjuk ke data lengkap;
        # yang dirantai adalah fakta-fakta yang membuktikan APA yang diputuskan.
        # query_text dipotong: rantai adalah bukti keputusan, bukan arsip kedua
        # dari isi percakapan (yang sudah ada di routing_events/session_turns).
        await self.chain.append(
            ENTRY_ROUTING_DECISION,
            {
                "session_id": session_id,
                "role": role,
                "user_id": user_id,
                "model": route.model,
                "provider": route.provider,
                "complexity": route.complexity.value,
                "complexity_score": route.complexity_score,
                "query_preview": query[:200],
            },
            ref_table="routing_events",
            ref_id=event_id,
        )
        return event_id

    async def finalize(self, event_id: int, turn, evidence: dict | None = None) -> None:
        """Update tokens, cost, latency, fallback_used, dan evidence setelah turn selesai.

        `evidence` (opsional, § Evidence-Based Response TODO.md Prioritas 2): snapshot
        policy/skill/guardrail yang berlaku saat turn ini — disimpan sebagai JSON agar
        query-able via GET /evidence/{event_id}, bukan cuma tersirat lintas kolom lain.
        """
        await self.db.execute(
            """
            UPDATE routing_events
            SET tokens_in=?, tokens_out=?, cost_usd=?, latency_ms=?, fallback_used=?,
                evidence_json=?
            WHERE id=?
            """,
            (
                turn.tokens_in,
                turn.tokens_out,
                turn.cost_usd,
                turn.latency_ms,
                int(getattr(turn, "fallback_used", False)),
                json.dumps(evidence) if evidence is not None else None,
                event_id,
            ),
        )
        # Rantai audit: catat HASIL turn sebagai entry BARU (bukan mengubah entry
        # keputusan) — urutan "diputuskan → diselesaikan" jadi terlihat sebagai
        # sejarah yang bisa diaudit, bukan satu baris yang ditimpa.
        await self.chain.append(
            ENTRY_ROUTING_FINALIZED,
            {
                "tokens_in": turn.tokens_in,
                "tokens_out": turn.tokens_out,
                "cost_usd": turn.cost_usd,
                "latency_ms": turn.latency_ms,
                "fallback_used": int(getattr(turn, "fallback_used", False)),
                "has_evidence": evidence is not None,
            },
            ref_table="routing_events",
            ref_id=event_id,
        )

    async def check_correction(self, user_message: str, session_id: str) -> bool:
        """
        Dipanggil di AWAL turn berikutnya. Deteksi apakah turn sebelumnya dikoreksi user.

        Return True bila pesan ini mengoreksi turn sebelumnya (dipakai SkillFeedback
        untuk memutuskan outcome skill turn lalu: refine/reset vs revive/promote).
        """
        msg = user_message.lower()
        if not any(sig in msg for sig in CORRECTION_SIGNALS):
            return False
        await self.db.execute(
            """
            UPDATE routing_events SET had_correction=1, correction_detail=?
            WHERE id = (SELECT id FROM routing_events
                        WHERE session_id=? ORDER BY id DESC LIMIT 1)
            """,
            (user_message[:200], session_id),
        )
        return True

    async def calibration_report(self) -> list[dict]:
        """Complexity label mana yang sering memicu koreksi → router under-provisioned."""
        return await self.db.fetchall(
            """
            SELECT complexity_label,
                   COUNT(*) as total,
                   SUM(had_correction) as corrections,
                   ROUND(100.0 * SUM(had_correction) / COUNT(*), 1) as correction_rate,
                   ROUND(AVG(cost_usd), 5) as avg_cost
            FROM routing_events
            GROUP BY complexity_label
            ORDER BY correction_rate DESC
            """
        )

    async def role_report(self) -> list[dict]:
        """Runtime Evaluation Engine (TODO.md § Prioritas 2): KPI per-role/agent —
        buyer enterprise cari dashboard "agent mana yang paling akurat/mahal/lambat",
        bukan cuma agregat per complexity_label (`calibration_report`). Dipakai `/metrics`.

        `avg_human_feedback` dihitung hanya dari event yang PUNYA rating (AVG SQL
        otomatis mengabaikan NULL) — NULL di sini berarti "belum ada yang menilai",
        beda dari 0 yang berarti "dinilai buruk".
        """
        return await self.db.fetchall(
            """
            SELECT role,
                   COUNT(*) as total,
                   SUM(had_correction) as corrections,
                   ROUND(100.0 * SUM(had_correction) / COUNT(*), 1) as correction_rate,
                   ROUND(AVG(cost_usd), 5) as avg_cost,
                   ROUND(AVG(latency_ms)) as avg_latency_ms,
                   ROUND(AVG(human_feedback), 2) as avg_human_feedback
            FROM routing_events
            GROUP BY role
            ORDER BY correction_rate DESC
            """
        )

    async def set_human_feedback(self, event_id: int, rating: int) -> bool:
        """Runtime Evaluation Engine: simpan rating eksplisit user (1-5) untuk satu
        turn — beda dari `had_correction` (sinyal IMPLISIT dari kata di pesan
        berikutnya). Return False (tanpa menulis apa pun) bila rating di luar
        rentang atau event_id tidak ditemukan — caller (endpoint) yang menerjemahkan
        ini jadi 400/404, bukan exception di sini.
        """
        if not 1 <= rating <= 5:
            return False
        cursor = await self.db.execute(
            "UPDATE routing_events SET human_feedback=? WHERE id=?",
            (rating, event_id),
        )
        return cursor.rowcount > 0

    async def cost_savings_report(self) -> dict:
        """Estimasi penghematan dari hybrid routing (§ Prioritas 9.4).

        TIDAK memakai kolom `routing_events.cost_usd` — kolom itu SELALU 0.0
        untuk setiap baris (lihat `core/router.py::MODELS` dan
        `core/router_config.py::get_map`, keduanya sengaja "cost nyata tak
        dipetakan; jangan tebak" untuk keputusan routing LIVE). Di sini kita
        hitung ulang dari `model_chosen` + token aktual via tarif publik
        (`core/cost_pricing.py`) — jelas dilabeli estimasi, bukan tagihan.

        Counterfactual = biaya seandainya token yang SAMA diproses model tier
        CRITICAL default (`SmartRouter.MODELS`, BUKAN peta yang mungkin
        di-override lewat `/router`) — baseline tetap, supaya angka
        penghematan tak bergeser di bawah kaki hanya karena operator mengubah
        konfigurasi model tanpa ada turn baru.

        Baris dengan `model_chosen` di luar tabel harga DIKELUARKAN dari
        agregat (`turns_unpriced`), bukan dianggap gratis — konsisten prinsip
        "jangan tebak" yang sama.
        """
        rows = await self.db.fetchall(
            "SELECT model_chosen, tokens_in, tokens_out FROM routing_events "
            "WHERE tokens_in IS NOT NULL AND tokens_out IS NOT NULL"
        )
        baseline_model = SmartRouter.MODELS[Complexity.CRITICAL][0]

        actual_total = 0.0
        counterfactual_total = 0.0
        counted = 0
        unpriced = 0
        for row in rows:
            actual = estimate_cost_usd(row["model_chosen"], row["tokens_in"], row["tokens_out"])
            counterfactual = estimate_cost_usd(baseline_model, row["tokens_in"], row["tokens_out"])
            if actual is None or counterfactual is None:
                unpriced += 1
                continue
            actual_total += actual
            counterfactual_total += counterfactual
            counted += 1

        savings = counterfactual_total - actual_total
        savings_pct = (savings / counterfactual_total * 100) if counterfactual_total > 0 else 0.0

        return {
            "is_estimate": True,  # selalu True — flag eksplisit, cegah UI merender sebagai angka pasti
            "turns_counted": counted,
            "turns_unpriced": unpriced,
            "baseline_model": baseline_model,
            "actual_cost_usd": round(actual_total, 4),
            "counterfactual_cost_usd": round(counterfactual_total, 4),
            "estimated_savings_usd": round(savings, 4),
            "estimated_savings_pct": round(savings_pct, 1),
            "pricing_verified_on": PRICING_VERIFIED_ON.isoformat(),
        }
