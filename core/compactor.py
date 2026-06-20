from collections.abc import Awaitable, Callable

# Tipe summarizer: terima teks history gabungan → kembalikan ringkasan (string).
# Di-inject dari agent_loop (membungkus LLM) agar compactor tetap extractable & bisa
# di-test tanpa LLM nyata (§5). None → tak ada compaction (truncation lama).
Summarizer = Callable[[str], Awaitable[str]]

# Penanda turn ringkasan di history agar tak diringkas dua kali & dikenali UI/log.
COMPACTION_MARKER = "[compacted]"


def _estimate_tokens(text: str) -> int:
    """Heuristik: ~4 karakter per token. Cukup akurat untuk gating, tanpa dependency tiktoken."""
    return len(text) // 4


class ContextCompactor:
    """Bangun messages list dengan batas token. Potong history lama jika perlu.
    Token-first: target < max_tokens (default 28K per CLAUDE.md §1).

    Dua strategi saat budget habis:
      - default: `build()` MEMOTONG turn lama (truncation) — bodoh tapi jujur, tak
        ada yang dikarang.
      - opt-in: `compact()` MERINGKAS turn lama jadi satu blok (terinspirasi headroom)
        sebelum `build()` — hemat token tanpa kehilangan konteks total. Dipanggil dari
        agent_loop HANYA bila /settings mengaktifkan (off|local|cloud).
    """

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens

    async def compact(
        self,
        history: list,
        summarizer: Summarizer,
        *,
        keep_recent: int = 4,
        min_old_turns: int = 3,
        reserve_tokens: int = 0,
    ) -> list:
        """Ringkas turn lama jadi satu turn ringkasan bila history melebihi budget.

        Mengembalikan history BARU (tak mengubah input): bila tak perlu/aman, kembalikan
        history apa adanya. Hanya berjalan bila:
          - jumlah turn lama (di luar `keep_recent`) ≥ `min_old_turns`, DAN
          - estimasi token history > budget (max_tokens − reserve_tokens).

        Fail-safe (§1.3): summarizer error/ringkasan kosong → kembalikan history asli
        (build() lalu truncation seperti biasa). Tak pernah crash turn.
        """
        turns = [t for t in history if getattr(t, "role", None) in ("user", "assistant")]
        if len(turns) <= keep_recent:
            return history
        budget = max(self.max_tokens - reserve_tokens, 0)
        total = sum(_estimate_tokens(getattr(t, "content", "") or "") for t in turns)
        if total <= budget:
            return history  # muat tanpa compaction → jangan keluarkan biaya LLM

        old = turns[:-keep_recent]
        recent = turns[-keep_recent:]
        # Jangan ringkas blok yang sudah berupa ringkasan (idempoten) atau terlalu kecil.
        already = any(getattr(t, "content", "").startswith(COMPACTION_MARKER) for t in old)
        if already or len(old) < min_old_turns:
            return history

        joined = "\n\n".join(
            f"{getattr(t, 'role', '?')}: {getattr(t, 'content', '') or ''}" for t in old
        )
        try:
            summary = (await summarizer(joined)).strip()
        except Exception:  # noqa: BLE001 — peringkasan gagal → fallback truncation
            return history
        if not summary:
            return history

        # Rekonstruksi history: turn non-(user/assistant) dipertahankan posisinya di awal
        # (mis. system tak ada di history di sini), lalu 1 turn ringkasan + recent.
        SummaryTurn = type(recent[-1])  # gunakan kelas Turn yang sama dari history
        summary_turn = SummaryTurn(role="assistant", content=f"{COMPACTION_MARKER} {summary}")
        others = [t for t in history if getattr(t, "role", None) not in ("user", "assistant")]
        return [*others, summary_turn, *recent]

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

        # I5: profil user naratif (blok stabil → cocok prompt-caching). Hanya bila ada.
        if memory.get("user_model"):
            parts.append(f"\n## User\n{memory['user_model']}")

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
