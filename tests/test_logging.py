"""Tests untuk infra/logging.py — setup structlog JSON renderer."""

import json

import structlog

from infra.logging import log, setup_logging


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
