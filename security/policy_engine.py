"""Policy Engine sederhana (TODO.md § Prioritas 3) — lapisan kondisi TAMBAHAN
di atas mekanisme allow-list (`soul.toml [tools] allowed`) dan approval statis
(`Tool.requires_approval`) yang sudah ada. TIDAK menggantikan keduanya.

Kondisi berbasis nested dict/TOML (BUKAN DSL string/eval Python) — keputusan
desain sadar: parser ekspresi kustom menambah permukaan bug/kerentanan yang
lebih mahal diverifikasi benar dibanding operator tetap per tipe field.
Konsisten prinsip minimalis CLAUDE.md §8.

Skema di soul.toml:
    [policy.file_write]
    deny_if = [{ field = "path", op = "prefix", value = "/etc" }]

    [policy.http_request]
    approval_required_if = [{ field = "url", op = "not_prefix", value = "https://api.internal" }]

`deny_if` SELALU menang atas `approval_required_if` bila keduanya match
(fail-safe: penolakan > permintaan approval, CLAUDE.md §1). Field yang
dicek kondisi tapi tidak ada di tool_input, atau operator tak dikenal
(typo config) → kondisi itu dianggap TIDAK match, bukan crash — config
yang salah tidak boleh menjatuhkan tool loop.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class PolicyDecision:
    action: str  # "allow" | "deny" | "require_approval"
    reason: str = ""


def _match(condition: dict, tool_input: dict) -> bool:
    """Evaluasi SATU kondisi terhadap tool_input. Return False (tidak match)
    untuk field hilang atau operator tak dikenal — fail-safe, tidak crash."""
    field = condition.get("field")
    op = condition.get("op")
    expected = condition.get("value")

    # "always" tidak butuh field di tool_input sama sekali — dipakai manifest
    # clawn.yaml untuk override tanpa syarat (§ infra/manifest.py), mis.
    # "approval: shell_run: required" tanpa kondisi spesifik.
    if op == "always":
        return True

    if field not in tool_input:
        return False
    actual: Any = tool_input[field]

    try:
        if op == "prefix":
            return str(actual).startswith(str(expected))
        if op == "not_prefix":
            return not str(actual).startswith(str(expected))
        if op == "contains":
            return str(expected) in str(actual)
        if op == "eq":
            return actual == expected
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
    except TypeError:
        # Tipe tidak sebanding (mis. bandingkan angka dengan string di config
        # yang salah tulis) — fail-safe: kondisi dianggap tidak match.
        return False
    return False  # operator tak dikenal


def _any_match(conditions: list[dict], tool_input: dict) -> tuple[bool, str]:
    """OR semantics: kondisi PERTAMA yang match menentukan hasil (aman-dulu —
    satu kondisi terpenuhi sudah cukup memicu deny/approval)."""
    for cond in conditions:
        if _match(cond, tool_input):
            field = cond.get("field", "?")
            op = cond.get("op", "?")
            value = cond.get("value", "?")
            return True, f"{field} {op} {value}"
    return False, ""


class PolicyEngine:
    """Evaluasi kondisi policy per-role, dibaca dari `soul.toml [policy.<tool>]`.

    Dipanggil di `AgentLoop._execute_tool` SEBELUM approval check & eksekusi
    (§ prasyarat "runtime, bukan library" TREND.md) — policy yang deny
    menghentikan tool sebelum sempat memicu approval sama sekali.
    """

    def __init__(self, policy_cfg: dict):
        """`policy_cfg` = dict `soul["policy"]` (bisa kosong `{}` bila role
        tidak punya section [policy] sama sekali — semua tool ALLOW default,
        perilaku lama tak berubah)."""
        self.policy_cfg = policy_cfg or {}

    def evaluate(self, tool_name: str, tool_input: dict) -> PolicyDecision:
        rules = self.policy_cfg.get(tool_name)
        if not rules:
            return PolicyDecision(action="allow")

        deny_conditions = rules.get("deny_if", [])
        matched, reason = _any_match(deny_conditions, tool_input)
        if matched:
            return PolicyDecision(action="deny", reason=f"policy deny_if: {reason}")

        approval_conditions = rules.get("approval_required_if", [])
        matched, reason = _any_match(approval_conditions, tool_input)
        if matched:
            return PolicyDecision(
                action="require_approval", reason=f"policy approval_required_if: {reason}"
            )

        return PolicyDecision(action="allow")
