"""Test untuk security/policy_engine.py — Policy Engine sederhana (TODO.md §
Prioritas 3). Kondisi berbasis nested dict/TOML (bukan DSL string) — operator
tetap per tipe field (path=prefix/contains, angka=perbandingan), sesuai
keputusan desain: hindari parser ekspresi kustom (risiko bug parsing) demi
kesederhanaan & keamanan yang lebih mudah diverifikasi.
"""

from security.policy_engine import PolicyDecision, PolicyEngine


def test_no_policy_configured_allows_by_default():
    """Tool tanpa section [policy.<tool>] sama sekali → ALLOW (perilaku lama
    tak berubah, policy adalah lapisan TAMBAHAN bukan pengganti mekanisme
    allow-list/approval yang sudah ada)."""
    engine = PolicyEngine(policy_cfg={})
    decision = engine.evaluate("file_write", {"path": "/home/user/x.txt"})
    assert decision.action == "allow"


def test_deny_if_prefix_match_denies():
    cfg = {
        "file_write": {
            "deny_if": [{"field": "path", "op": "prefix", "value": "/etc"}],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("file_write", {"path": "/etc/passwd"})
    assert decision.action == "deny"
    assert "path" in decision.reason


def test_deny_if_prefix_no_match_allows():
    cfg = {
        "file_write": {
            "deny_if": [{"field": "path", "op": "prefix", "value": "/etc"}],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("file_write", {"path": "/home/user/x.txt"})
    assert decision.action == "allow"


def test_deny_if_gt_denies_when_exceeded():
    cfg = {
        "http_request": {
            "deny_if": [{"field": "timeout", "op": "gt", "value": 300}],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("http_request", {"timeout": 500})
    assert decision.action == "deny"


def test_deny_if_gt_allows_when_under():
    cfg = {
        "http_request": {
            "deny_if": [{"field": "timeout", "op": "gt", "value": 300}],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("http_request", {"timeout": 100})
    assert decision.action == "allow"


def test_approval_required_if_condition_met():
    cfg = {
        "http_request": {
            "approval_required_if": [
                {"field": "url", "op": "not_prefix", "value": "https://api.internal"}
            ],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("http_request", {"url": "https://evil.example.com"})
    assert decision.action == "require_approval"


def test_approval_required_if_condition_not_met():
    cfg = {
        "http_request": {
            "approval_required_if": [
                {"field": "url", "op": "not_prefix", "value": "https://api.internal"}
            ],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("http_request", {"url": "https://api.internal/foo"})
    assert decision.action == "allow"


def test_deny_takes_priority_over_approval_required():
    """Bila deny_if DAN approval_required_if sama-sama match → deny menang
    (fail-safe: penolakan lebih kuat daripada meminta approval, CLAUDE.md §1)."""
    cfg = {
        "file_write": {
            "deny_if": [{"field": "path", "op": "prefix", "value": "/etc"}],
            "approval_required_if": [{"field": "path", "op": "contains", "value": "etc"}],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("file_write", {"path": "/etc/passwd"})
    assert decision.action == "deny"


def test_contains_operator():
    cfg = {"web_fetch": {"deny_if": [{"field": "url", "op": "contains", "value": "localhost"}]}}
    engine = PolicyEngine(policy_cfg=cfg)
    assert engine.evaluate("web_fetch", {"url": "http://localhost:8000"}).action == "deny"
    assert engine.evaluate("web_fetch", {"url": "http://example.com"}).action == "allow"


def test_gte_lte_operators():
    cfg = {
        "db_query": {
            "deny_if": [{"field": "limit", "op": "gte", "value": 1000}],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    assert engine.evaluate("db_query", {"limit": 1000}).action == "deny"
    assert engine.evaluate("db_query", {"limit": 999}).action == "allow"


def test_lt_operator():
    cfg = {"tool_x": {"deny_if": [{"field": "n", "op": "lt", "value": 5}]}}
    engine = PolicyEngine(policy_cfg=cfg)
    assert engine.evaluate("tool_x", {"n": 3}).action == "deny"
    assert engine.evaluate("tool_x", {"n": 5}).action == "allow"


def test_eq_operator():
    cfg = {"tool_x": {"deny_if": [{"field": "mode", "op": "eq", "value": "danger"}]}}
    engine = PolicyEngine(policy_cfg=cfg)
    assert engine.evaluate("tool_x", {"mode": "danger"}).action == "deny"
    assert engine.evaluate("tool_x", {"mode": "safe"}).action == "allow"


def test_missing_field_in_tool_input_does_not_match_condition():
    """Field yang dicek kondisi tidak ada di tool_input → kondisi dianggap
    TIDAK match (fail-safe: tidak bisa evaluasi = tidak menolak secara keliru,
    tapi juga tidak salah mengizinkan sesuatu yang seharusnya dicek — field
    hilang berarti tool_input tidak lengkap, ditangkap validasi schema lain)."""
    cfg = {"tool_x": {"deny_if": [{"field": "amount", "op": "gt", "value": 100}]}}
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("tool_x", {})
    assert decision.action == "allow"


def test_unknown_operator_is_ignored_fail_safe():
    """Operator tak dikenal (typo config) → kondisi itu diabaikan, tidak crash.
    Config yang salah tidak boleh menjatuhkan seluruh tool loop."""
    cfg = {"tool_x": {"deny_if": [{"field": "n", "op": "typo_operator", "value": 5}]}}
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("tool_x", {"n": 999})
    assert decision.action == "allow"


def test_multiple_deny_conditions_any_match_denies():
    """Beberapa kondisi di deny_if — cukup SATU yang match untuk deny (OR
    semantics, konsisten sikap fail-safe/aman-dulu)."""
    cfg = {
        "file_write": {
            "deny_if": [
                {"field": "path", "op": "prefix", "value": "/etc"},
                {"field": "path", "op": "prefix", "value": "/root"},
            ],
        }
    }
    engine = PolicyEngine(policy_cfg=cfg)
    assert engine.evaluate("file_write", {"path": "/root/.ssh/id_rsa"}).action == "deny"
    assert engine.evaluate("file_write", {"path": "/home/user/ok.txt"}).action == "allow"


def test_always_operator_matches_without_field_in_tool_input():
    """ "always" (§ infra/manifest.py clawn.yaml "approval: <tool>: required"
    tanpa kondisi spesifik) match tanpa perlu field apa pun ada di tool_input."""
    cfg = {"shell_run": {"approval_required_if": [{"op": "always"}]}}
    engine = PolicyEngine(policy_cfg=cfg)
    decision = engine.evaluate("shell_run", {"command": "ls"})
    assert decision.action == "require_approval"


def test_policy_decision_is_dataclass_with_reason():
    decision = PolicyDecision(action="deny", reason="test")
    assert decision.action == "deny"
    assert decision.reason == "test"
