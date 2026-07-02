"""Test untuk security/auth.py — session token signing, verifikasi, CSRF token.

Security-critical: signature harus tolak token dipalsukan/kedaluwarsa/secret salah.
"""

import time

from security.auth import (
    SESSION_MAX_AGE_SEC,
    _sign,
    create_session_token,
    generate_csrf_token,
    is_public_path,
    verify_login_token,
    verify_session_token,
)


def test_valid_token_verifies():
    token = create_session_token("secret123")
    assert verify_session_token(token, "secret123") is True


def test_wrong_secret_rejected():
    token = create_session_token("secret123")
    assert verify_session_token(token, "wrong-secret") is False


def test_none_token_rejected():
    assert verify_session_token(None, "secret123") is False


def test_empty_token_rejected():
    assert verify_session_token("", "secret123") is False


def test_malformed_token_no_dot_rejected():
    assert verify_session_token("not-a-valid-token", "secret123") is False


def test_tampered_timestamp_rejected():
    """Ubah timestamp tanpa mengubah signature → signature tak lagi cocok."""
    token = create_session_token("secret123")
    ts, _, sig = token.partition(".")
    tampered = f"{int(ts) + 1000}.{sig}"
    assert verify_session_token(tampered, "secret123") is False


def test_tampered_signature_rejected():
    token = create_session_token("secret123")
    ts, _, sig = token.partition(".")
    tampered = f"{ts}.{'0' * len(sig)}"
    assert verify_session_token(tampered, "secret123") is False


def test_expired_token_rejected():
    """Token dengan timestamp di luar SESSION_MAX_AGE_SEC harus ditolak."""
    old_ts = str(int(time.time()) - SESSION_MAX_AGE_SEC - 10)
    forged = f"{old_ts}.{_sign(old_ts, 'secret123')}"
    assert verify_session_token(forged, "secret123") is False


def test_future_timestamp_rejected():
    """Timestamp di masa depan (clock skew ekstrem/serangan) juga ditolak."""
    future_ts = str(int(time.time()) + 3600)
    forged = f"{future_ts}.{_sign(future_ts, 'secret123')}"
    assert verify_session_token(forged, "secret123") is False


def test_non_numeric_timestamp_rejected():
    assert verify_session_token("abc.somesignature", "secret123") is False


def test_login_token_matches():
    assert verify_login_token("mypassword", "mypassword") is True


def test_login_token_mismatch():
    assert verify_login_token("wrong", "mypassword") is False


def test_csrf_token_is_random_and_url_safe():
    a = generate_csrf_token()
    b = generate_csrf_token()
    assert a != b
    assert len(a) > 20


def test_public_paths_allowed_without_session():
    assert is_public_path("/health") is True
    assert is_public_path("/login") is True
    assert is_public_path("/static/style.css") is True
    assert is_public_path("/static/css/base.css") is True


def test_protected_paths_not_public():
    assert is_public_path("/") is False
    assert is_public_path("/skills") is False
    assert is_public_path("/chat/stream") is False


def test_max_age_sec_default_matches_session_max_age():
    """Tanpa argumen eksplisit, perilaku lama tak berubah (absolute expiry 7 hari)."""
    token = create_session_token("secret123")
    assert verify_session_token(token, "secret123") is True


def test_custom_max_age_sec_rejects_token_older_than_it():
    """Idle timeout: max_age_sec lebih ketat dari absolute expiry harus ditolak."""
    old_ts = str(int(time.time()) - 100)
    token = f"{old_ts}.{_sign(old_ts, 'secret123')}"
    assert verify_session_token(token, "secret123", max_age_sec=SESSION_MAX_AGE_SEC) is True
    assert verify_session_token(token, "secret123", max_age_sec=50) is False


def test_custom_max_age_sec_accepts_token_within_window():
    ts = str(int(time.time()) - 10)
    token = f"{ts}.{_sign(ts, 'secret123')}"
    assert verify_session_token(token, "secret123", max_age_sec=60) is True
