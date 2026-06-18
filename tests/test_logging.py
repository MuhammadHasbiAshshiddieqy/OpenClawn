"""Tests untuk infra/logging.py — setup structlog JSON renderer + secret-scrubbing."""

import json

import structlog

from infra.logging import log, scrub_secrets, setup_logging


def test_setup_logging_idempotent():
    """setup_logging() bisa dipanggil berkali-kali tanpa error (dipanggil saat startup)."""
    setup_logging()
    setup_logging()  # tidak boleh raise


def test_log_is_callable_logger():
    """`log` adalah logger structlog yang punya method level standar."""
    for level in ("debug", "info", "warning", "error"):
        assert hasattr(log, level)


def test_setup_logging_renders_json(capsys):
    """Setelah setup, output log berupa JSON satu baris dengan level + event.

    Menangkap regresi nyata: bila JSONRenderer/add_log_level dilepas dari
    processor chain, format ini akan rusak.
    """
    setup_logging()
    structlog.get_logger().info("unit_test_event", foo="bar", n=1)

    captured = capsys.readouterr()
    line = (captured.out or captured.err).strip().splitlines()[-1]
    parsed = json.loads(line)  # harus JSON valid
    assert parsed["event"] == "unit_test_event"
    assert parsed["foo"] == "bar"
    assert parsed["level"] == "info"
    assert "timestamp" in parsed  # TimeStamper aktif


# ── secret-scrubbing (§1.2 defense-in-depth) ──────────────────────────────────


def test_scrub_redacts_sensitive_key_names():
    """Field bernama api_key/token/secret/password di-redact penuh."""
    out = scrub_secrets(None, "info", {"event": "x", "api_key": "rahasia123", "token": "abc"})
    assert out["api_key"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert out["event"] == "x"  # field biasa tak tersentuh


def test_scrub_redacts_secret_patterns_in_values():
    """Nilai berpola secret (sk-, bearer, gh token) di-redact meski nama field biasa."""
    out = scrub_secrets(
        None,
        "info",
        {
            "msg": "gagal pakai sk-abcdEFGH1234567890xyz tadi",
            "hdr": "Authorization: Bearer ghp_ABCDEFGHIJKLMNOP1234",
        },
    )
    assert "sk-abcd" not in out["msg"]
    assert "[REDACTED]" in out["msg"]
    assert "ghp_" not in out["hdr"]


def test_scrub_leaves_normal_values():
    """Teks biasa tidak diubah (tak ada false-positive yang merusak log normal)."""
    out = scrub_secrets(None, "info", {"event": "routing", "model": "gemma4:e4b", "n": 3})
    assert out["model"] == "gemma4:e4b"
    assert out["n"] == 3


def test_scrub_fail_soft_on_bad_input():
    """Input aneh tidak meledak — logging tak boleh gagal karena scrub."""
    out = scrub_secrets(None, "info", {"event": "x", "obj": object()})
    assert out["event"] == "x"


def test_setup_logging_scrubs_in_pipeline(capsys):
    """End-to-end: secret di field sensitif tak muncul di output JSON."""
    setup_logging()
    structlog.get_logger().info("auth_attempt", api_key="sk-supersecretvalue123456")
    line = (capsys.readouterr().out or "").strip().splitlines()[-1]
    assert "supersecret" not in line
    assert "[REDACTED]" in line
