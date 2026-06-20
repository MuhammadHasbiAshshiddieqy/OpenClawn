import tomllib
from dataclasses import dataclass
from enum import Enum

from infra.config import CONFIG, AppConfig


class Complexity(Enum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    CRITICAL = "critical"


@dataclass
class RouteDecision:
    model: str
    provider: str
    complexity: Complexity
    complexity_score: int
    reason: str
    cost_per_1k: float
    dimensions: dict
    soul_upgrade_hit: bool


class SmartRouter:
    """
    Soul-aware router. Audit #1: membaca soul.toml role aktif saat __init__,
    bukan tiap request.
    - upgrade_keywords dari soul menambah skor (+3)
    - prefer_local menaikkan threshold upgrade ke Claude (+1)
    """

    # Setup utama LOKAL: tier lokal dibedakan per kapasitas model — makin sulit
    # case, makin mampu model. gemma4:e4b (ringan) → deepseek-r1 → qwen3.5:9b
    # (paling mampu lokal). Tier berat naik ke Gemini (cloud).
    MODELS: dict[Complexity, tuple[str, str, float]] = {
        Complexity.TRIVIAL: ("gemma4:e4b", "ollama", 0.0),
        Complexity.SIMPLE: ("deepseek-r1:latest", "ollama", 0.0),
        Complexity.MODERATE: ("qwen3.5:9b", "ollama", 0.0),
        Complexity.COMPLEX: ("gemini-2.5-flash", "gemini", 0.0),
        Complexity.CRITICAL: ("gemini-2.5-pro", "gemini", 0.0),
    }

    def __init__(
        self,
        role: str,
        soul_path: str | None = None,
        threshold_offset: int = 0,
        config: AppConfig = CONFIG,
    ):
        self.role = role
        soul = self._load_soul(role, soul_path)
        routing_cfg = soul.get("routing", {})
        self.prefer_local: bool = routing_cfg.get("prefer_local", False)
        self.soul_upgrade_kw: list[str] = routing_cfg.get("upgrade_keywords", [])
        # Keyword routing dari config (§1.5: tak hardcoded locale di core) + ekstra
        # locale-spesifik dari soul.toml [routing] (tech_keywords/multistep_keywords/
        # urgency_keywords). Digabung & di-lowercase sekali agar deteksi multibahasa.
        self.tech_kw = self._merge_kw(config.routing_tech_keywords, routing_cfg, "tech_keywords")
        self.multi_kw = self._merge_kw(
            config.routing_multistep_keywords, routing_cfg, "multistep_keywords"
        )
        self.urgency_kw = self._merge_kw(
            config.routing_urgency_keywords, routing_cfg, "urgency_keywords"
        )
        # Audit #1 (loop tertutup): offset global hasil kalibrasi. Negatif = router
        # naik tier lebih cepat (perbaiki under-provisioning); positif = bertahan di
        # tier murah lebih lama (perbaiki over-provisioning). Default 0 = router asli.
        # Diterapkan di _label() bersama threshold_shift dari prefer_local.
        self.threshold_offset: int = threshold_offset
        # Peta tier→(model, provider, cost) yang AKTIF. Default = MODELS hardcoded,
        # tapi bisa di-override per-turn dari DB (RouterConfigStore) agar user memilih
        # model tiap tier lewat /router tanpa mengubah kode. Router tetap memutuskan
        # TIER; peta ini hanya menentukan MODEL untuk tier itu.
        self.model_map: dict[Complexity, tuple[str, str, float]] = dict(self.MODELS)

    @staticmethod
    def _merge_kw(defaults: tuple, routing_cfg: dict, soul_key: str) -> list[str]:
        """Gabung keyword default (config) + ekstra locale dari soul, lowercase, dedup."""
        extra = routing_cfg.get(soul_key, []) or []
        return sorted({str(k).lower() for k in (*defaults, *extra)})

    def _load_soul(self, role: str, soul_path: str | None) -> dict:
        path = soul_path or f"roles/{role}/soul.toml"
        with open(path, "rb") as f:
            return tomllib.load(f)

    def decide(self, messages: list, query: str) -> RouteDecision:
        dims = self._dimensions(messages, query)
        soul_hit = any(k.lower() in query.lower() for k in self.soul_upgrade_kw)
        dims["soul_upgrade_hit"] = int(soul_hit)

        score = self._score(dims)

        # Audit #1: soul upgrade_keywords memaksa naik kompleksitas.
        # Soul hit bypass prefer_local — soul memiliki prioritas lebih tinggi.
        if soul_hit:
            score += 3

        # prefer_local menaikkan threshold, tapi tidak berlaku saat soul override aktif
        threshold_shift = (1 if self.prefer_local else 0) if not soul_hit else 0
        # Offset kalibrasi selalu berlaku (bahkan saat soul hit) — ia menyetel
        # perilaku router secara global berdasar bukti correction-rate, bukan keyword.
        threshold_shift += self.threshold_offset

        complexity = self._label(score, threshold_shift)
        # Pakai peta aktif (default MODELS, atau override dari /router); fallback ke
        # MODELS bila tier tak ada di peta override (jaga-jaga peta korup/parsial).
        model, provider, cost = self.model_map.get(complexity, self.MODELS[complexity])

        return RouteDecision(
            model=model,
            provider=provider,
            complexity=complexity,
            complexity_score=score,
            reason=self._explain(complexity, soul_hit),
            cost_per_1k=cost,
            dimensions=dims,
            soul_upgrade_hit=soul_hit,
        )

    def _dimensions(self, messages: list, query: str) -> dict:
        q = query.lower()
        return {
            "query_tokens": int(len(query.split()) * 1.3),
            "has_tech_kw": int(any(k in q for k in self.tech_kw)),
            "needs_multistep": int(any(k in q for k in self.multi_kw)),
            "history_len": len(messages),
            "role": self.role,
            "has_urgency": int(any(k in q for k in self.urgency_kw)),
            "needs_stream": 1,
            "is_continuation": int(len(messages) > 2),
        }

    def _score(self, d: dict) -> int:
        s = 0
        if d["query_tokens"] > 200:
            s += 2
        elif d["query_tokens"] > 50:
            s += 1
        if d["has_tech_kw"]:
            s += 2
        if d["needs_multistep"]:
            s += 2
        if d["history_len"] > 10:
            s += 1
        if d["has_urgency"]:
            s += 1
        return s

    def _label(self, score: int, threshold_shift: int) -> Complexity:
        # threshold_shift menaikkan batas → prefer_local lebih lama bertahan di Ollama
        if score <= 1 + threshold_shift:
            return Complexity.TRIVIAL
        if score <= 2 + threshold_shift:
            return Complexity.SIMPLE
        if score <= 4 + threshold_shift:
            return Complexity.MODERATE
        if score <= 6 + threshold_shift:
            return Complexity.COMPLEX
        return Complexity.CRITICAL

    def _explain(self, c: Complexity, soul_hit: bool) -> str:
        base = {
            Complexity.TRIVIAL: "Greeting/singkat → Gemma4 e2b",
            Complexity.SIMPLE: "Sederhana → Gemma4 e4b",
            Complexity.MODERATE: "Menengah → Gemma4 12b",
            Complexity.COMPLEX: "Kompleks → Claude Haiku",
            Complexity.CRITICAL: "Kritis → Claude Sonnet",
        }[c]
        if soul_hit:
            base += " (dipicu soul upgrade_keyword)"
        return base
