"""Rate limiter in-memory (§P0 production-readiness) — sliding window per sesi.

Single-process, single-user by design (§7) — TIDAK butuh Redis/dependency
eksternal. Membatasi endpoint LLM (`/chat/stream`, `/converse/stream`) agar
biaya tak tak-terkendali & mencegah DoS sederhana saat self-host di VPS publik.

Window sliding sederhana: simpan timestamp request per key (session_id atau IP
fallback), buang yang di luar window tiap cek. Reset otomatis saat proses restart
(state in-memory, bukan persisten — dapat diterima untuk single-user).
"""

import time
from collections import defaultdict

# request per window per key — cukup longgar untuk pemakaian wajar (mis. beberapa
# turn cepat berturut-turut), ketat untuk mencegah spam/DoS otomatis.
DEFAULT_MAX_REQUESTS = 20
DEFAULT_WINDOW_SEC = 60


class RateLimiter:
    """Sliding window in-memory. `key` biasanya session_id, fallback client IP."""

    def __init__(
        self, max_requests: int = DEFAULT_MAX_REQUESTS, window_sec: int = DEFAULT_WINDOW_SEC
    ):
        self.max_requests = max_requests
        self.window_sec = window_sec
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        """True bila request boleh lanjut; False bila melebihi ambang window ini.

        Side-effect: mencatat hit INI ke window bila diizinkan (tidak mencatat
        hit yang ditolak, agar retry setelah window lewat tak ikut diblokir).
        """
        now = time.monotonic()
        cutoff = now - self.window_sec
        hits = self._hits[key]
        # Buang hit lama di luar window (housekeeping ringan, O(n) per key kecil).
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True

    def remaining(self, key: str) -> int:
        """Sisa kuota di window saat ini — untuk header X-RateLimit-Remaining."""
        now = time.monotonic()
        cutoff = now - self.window_sec
        hits = [h for h in self._hits[key] if h >= cutoff]
        return max(0, self.max_requests - len(hits))
