"""Audit 2026-09-26: scrubber log (CLAUDE.md §1.2 — credential tak boleh masuk log)."""

import structlog

from infra.logging import scrub_secrets


def test_usage_token_counts_are_not_redacted():
    """Hint 'token' SEBELUMNYA dicocokkan substring — tokens_in/max_tokens ikut
    [REDACTED], metrik penggunaan hilang dari log."""
    out = scrub_secrets(
        None, "info", {"tokens_in": 120, "max_tokens": 4096, "usage": {"input_tokens": 5}}
    )
    assert out["tokens_in"] == 120 and out["max_tokens"] == 4096
    assert out["usage"]["input_tokens"] == 5


def test_secret_named_fields_still_redacted():
    out = scrub_secrets(
        None,
        "info",
        {
            "access_token": "abc",
            "api_key": "x",
            "client_secret": "y",
            "headers": {"Authorization": "z"},
        },
    )
    assert out["access_token"] == out["api_key"] == out["client_secret"] == "[REDACTED]"
    assert out["headers"]["Authorization"] == "[REDACTED]"


def test_tavily_and_fine_grained_github_tokens_redacted():
    out = scrub_secrets(
        None,
        "error",
        {"error": "gagal tvly-AbCdEfGhIjKlMnOpQrSt dan github_pat_11ABCDEFG0123456789_abcdefghijk"},
    )
    assert "tvly-" not in out["error"] and "github_pat_" not in out["error"]


def test_scrubber_active_without_explicit_setup():
    """Skrip CLI (scripts/*.py) tak memanggil setup_logging() — SEBELUMNYA
    structlog jalan dengan konfigurasi default TANPA scrubber."""
    import infra.logging  # noqa: F401 — impor modul harus sudah memasang scrubber

    assert scrub_secrets in structlog.get_config()["processors"]
