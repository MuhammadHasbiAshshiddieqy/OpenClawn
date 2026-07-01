"""Test untuk security/rate_limit.py — sliding window in-memory."""

import time

from security.rate_limit import RateLimiter


def test_allows_up_to_max_requests():
    limiter = RateLimiter(max_requests=3, window_sec=60)
    assert limiter.allow("session-a") is True
    assert limiter.allow("session-a") is True
    assert limiter.allow("session-a") is True


def test_blocks_after_max_requests():
    limiter = RateLimiter(max_requests=2, window_sec=60)
    assert limiter.allow("session-a") is True
    assert limiter.allow("session-a") is True
    assert limiter.allow("session-a") is False


def test_different_keys_independent():
    limiter = RateLimiter(max_requests=1, window_sec=60)
    assert limiter.allow("session-a") is True
    assert limiter.allow("session-b") is True  # key berbeda, kuota terpisah
    assert limiter.allow("session-a") is False


def test_window_expiry_allows_again():
    """Hit di luar window tak lagi dihitung — pakai window sangat pendek untuk test cepat."""
    limiter = RateLimiter(max_requests=1, window_sec=0.05)
    assert limiter.allow("session-a") is True
    assert limiter.allow("session-a") is False
    time.sleep(0.06)
    assert limiter.allow("session-a") is True


def test_rejected_hit_not_counted():
    """Request yang DITOLAK tak ikut disimpan — begitu window lewat, kuota penuh lagi
    tanpa perlu menunggu hit yang ditolak itu sendiri kedaluwarsa."""
    limiter = RateLimiter(max_requests=1, window_sec=0.05)
    limiter.allow("session-a")  # pakai kuota
    limiter.allow("session-a")  # ditolak, tak dicatat
    limiter.allow("session-a")  # ditolak juga
    time.sleep(0.06)
    assert limiter.allow("session-a") is True


def test_remaining_reflects_usage():
    limiter = RateLimiter(max_requests=3, window_sec=60)
    assert limiter.remaining("session-a") == 3
    limiter.allow("session-a")
    assert limiter.remaining("session-a") == 2
    limiter.allow("session-a")
    limiter.allow("session-a")
    assert limiter.remaining("session-a") == 0


def test_remaining_never_negative():
    limiter = RateLimiter(max_requests=1, window_sec=60)
    limiter.allow("session-a")
    limiter.allow("session-a")  # ditolak
    assert limiter.remaining("session-a") == 0
