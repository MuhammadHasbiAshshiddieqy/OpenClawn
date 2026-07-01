"""Auth self-host (§P0 production-readiness) — single-user, shared-secret login.

TIDAK multi-user (§7: single-user by design). Hanya menjawab "apakah orang ini
tahu OPENCLAWN_AUTH_TOKEN", bukan sistem akun. Cocok untuk self-host di VPS
publik: mencegah orang lain yang sekadar reach port 8000 bisa chat/execute agent.

Session token murni stdlib (hmac + secrets) — TIDAK pakai itsdangerous/SessionMiddleware
Starlette (butuh dependency baru, di luar §7 tanpa persetujuan eksplisit). Pola sama
`Shield`/`Vault`: extractable, tanpa dependency di luar yang sudah final.

Desain:
- `OPENCLAWN_AUTH_TOKEN` di .env = password shared satu-satunya user.
- Kosong/tak diset → auth DIMATIKAN (fail-open ke perilaku lama, localhost dev tetap
  jalan tanpa login — perubahan ini opt-in, bukan breaking default).
- Login sukses → cookie `openclawn_session` berisi payload `{ts}.{hmac_hex}`,
  ditandatangani HMAC-SHA256 memakai OPENCLAWN_AUTH_TOKEN sebagai key. Verifikasi
  ulang signature + expiry (default 7 hari) di tiap request via middleware.
- TIDAK ada state sesi di server (stateless signed cookie) — restart server tak
  memaksa re-login selama cookie belum kedaluwarsa.
"""

import hashlib
import hmac
import secrets
import time

SESSION_COOKIE = "openclawn_session"
SESSION_MAX_AGE_SEC = 7 * 24 * 3600  # 7 hari

# Endpoint yang harus tetap bisa diakses TANPA login (health check monitoring,
# aset statis untuk merender halaman login itu sendiri, dan login flow itu sendiri).
PUBLIC_PATHS = {"/health", "/login"}
PUBLIC_PREFIXES = ("/static/",)


def is_public_path(path: str) -> bool:
    """True bila path boleh diakses tanpa sesi valid."""
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(secret: str) -> str:
    """Buat token sesi baru: `{timestamp}.{hmac_hex}`. Dipanggil saat login sukses."""
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts, secret)}"


def verify_session_token(token: str | None, secret: str) -> bool:
    """Verifikasi signature HMAC + expiry. Gagal parse/signature/expired → False.

    `hmac.compare_digest` mencegah timing attack saat membandingkan signature.
    """
    if not token or "." not in token:
        return False
    ts_str, _, sig = token.partition(".")
    if not ts_str.isdigit():
        return False
    if not hmac.compare_digest(sig, _sign(ts_str, secret)):
        return False
    age = time.time() - int(ts_str)
    return 0 <= age <= SESSION_MAX_AGE_SEC


def verify_login_token(candidate: str, secret: str) -> bool:
    """Bandingkan password yang diketik user vs OPENCLAWN_AUTH_TOKEN. Constant-time."""
    return hmac.compare_digest(candidate, secret)


def generate_csrf_token() -> str:
    """Token CSRF acak per sesi — disimpan di cookie terpisah + disuntik ke form."""
    return secrets.token_urlsafe(32)
