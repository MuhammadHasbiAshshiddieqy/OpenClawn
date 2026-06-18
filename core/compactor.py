def _estimate_tokens(text: str) -> int:
    """Heuristik: ~4 karakter per token. Cukup akurat untuk gating, tanpa dependency tiktoken."""
    return len(text) // 4


class ContextCompactor:
    """Bangun messages list dengan batas token. Potong history lama jika perlu.
    Token-first: target < max_tokens (default 28K per CLAUDE.md §1).
    """

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens

    def build(self, soul: str, memory: dict, history: list, user_message: str) -> list[dict]:
        system_content = self._build_system(soul, memory)
        messages: list[dict] = [{"role": "system", "content": system_content}]

        # Budget token yang tersisa setelah system prompt + user message
        used = _estimate_tokens(system_content) + _estimate_tokens(user_message)
        budget = self.max_tokens - used

        # Tambah history dari yang terbaru, hentikan jika token habis
        history_turns = [t for t in history[-20:] if t.role in ("user", "assistant")]
        kept: list[dict] = []
        for turn in reversed(history_turns):
            cost = _estimate_tokens(turn.content or "")
            if budget - cost < 0:
                break
            kept.append({"role": turn.role, "content": turn.content or ""})
            budget -= cost

        messages.extend(reversed(kept))
        messages.append({"role": "user", "content": user_message})
        return messages

    def estimate_context_tokens(self, messages: list[dict]) -> int:
        """Estimasi token total context window yang dikirim ke LLM (prompt-side).

        Token-first (§1.4): dipakai untuk meter budget di UI agar target < max_tokens
        terukur, bukan ditebak. Heuristik sama dengan trimming agar konsisten.
        """
        return sum(_estimate_tokens(m.get("content", "")) for m in messages)

    def _build_system(self, soul: str, memory: dict) -> str:
        parts = [soul]

        if memory.get("l1"):
            facts = "\n".join(f"- {k}: {v}" for k, v in memory["l1"].items())
            parts.append(f"\n## State\n{facts}")

        if memory.get("l2"):
            facts = "\n".join(f"- {f}" for f in memory["l2"][:10])
            parts.append(f"\n## Facts\n{facts}")

        if memory.get("l3"):
            skills = "\n".join(f"- {s['skill_name']}" for s in memory["l3"][:5])
            parts.append(f"\n## Active Skills\n{skills}")

        if memory.get("l4"):
            archives = "\n".join(f"- {s}" for s in memory["l4"][:3])
            parts.append(f"\n## Past Sessions\n{archives}")

        return "\n".join(parts)
