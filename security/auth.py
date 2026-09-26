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

Idle timeout (§ production-readiness, opt-in, TODO.md § Prioritas 1.5): `ts` di
token adalah waktu token DITERBITKAN, bukan waktu aktivitas terakhir — desain
stateless tidak punya "last seen" di server. `verify_session_token(max_age_sec=...)`
membiarkan pemanggil (middleware) memakai batas lebih ketat dari absolute expiry
default. Untuk idle timeout sungguhan (logout setelah N detik TAK aktif, bukan N
detik sejak login), middleware menerbitkan ULANG cookie (dengan `ts` baru) di
setiap request valid ketika `CONFIG.idle_timeout_sec` diisi — efektif menjadikan
`ts` sebagai "waktu aktivitas terakhir" sambil tetap stateless (tidak ada tabel
sesi baru di DB). Default `None` (OFF) → perilaku lama sama sekali tak berubah.
"""

import hashlib
import hmac
import secrets
import time

SESSION_COOKIE = "openclawn_session"
SESSION_MAX_AGE_SEC = 7 * 24 * 3600  # 7 hari

# Endpoint yang harus tetap bisa diakses TANPA login (health check monitoring,
# aset statis untuk merender halaman login itu sendiri, dan login flow itu sendiri).
# `/login/oidc` (redirect ke provider) dan `/auth/callback` (kembalian provider)
# TERMASUK — pengguna belum punya sesi sama sekali di titik ini (TODO.md § Prioritas 5).
# `/metrics/prometheus` (TODO.md § Prioritas 6) publik sama seperti `/health` —
# scraper Prometheus tak bawa cookie sesi; datanya murni agregat operasional
# (jumlah skill/tool-call/user per role), tak ada PII/kredensial.
PUBLIC_PATHS = {"/health", "/login", "/login/oidc", "/auth/callback", "/metrics/prometheus"}
PUBLIC_PREFIXES = ("/static/",)


def is_public_path(path: str) -> bool:
    """True bila path boleh diakses tanpa sesi valid."""
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(
    secret: str, user_id: int | None = None, issued_at: int | None = None
) -> str:
    """Buat token sesi: `{ts}.{user_id}.{iat}.{hmac_hex}`.

    `ts` = waktu aktivitas terakhir (di-refresh middleware bila idle timeout
    aktif); `iat` = waktu LOGIN asli, tak pernah berubah saat refresh.
    Audit 2026-09-25: sebelumnya token hanya punya `ts` — refresh idle timeout
    me-reset-nya, sehingga batas absolut `SESSION_MAX_AGE_SEC` (7 hari) tak
    pernah tercapai selama sesi (atau cookie curian) terus dipakai.
    `issued_at` None → sekarang (login baru).

    `user_id` (TODO.md § Prioritas 5, RBAC): id baris `infra.users.User` pemilik
    sesi — dibutuhkan middleware untuk memuat `request.state.user` tiap request
    tanpa query tambahan berbasis cookie lain. `None` (default, kompatibilitas
    mundur untuk pemanggil yang belum diupdate) → disimpan sebagai string kosong,
    `verify_session_token` mengembalikan `user_id=None` untuk token semacam ini.
    """
    now = int(time.time())
    ts = str(now)
    iat = str(issued_at if issued_at is not None else now)
    uid = str(user_id) if user_id is not None else ""
    payload = f"{ts}.{uid}.{iat}"
    return f"{payload}.{_sign(payload, secret)}"


def _parse(token: str | None) -> tuple[str, str, str, str] | None:
    """(ts, uid, iat, sig) — menerima format lama `{ts}.{uid}.{sig}` (iat=ts)."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) == 3:
        ts_str, uid_str, sig = parts
        return ts_str, uid_str, "", sig
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    return None


def token_issued_at(token: str | None) -> int | None:
    """Waktu login asli (`iat`) dari token yang SUDAH diverifikasi — dipakai
    middleware agar refresh idle timeout tak memperpanjang batas absolut."""
    parsed = _parse(token)
    if parsed is None:
        return None
    ts_str, _, iat_str, _ = parsed
    raw = iat_str or ts_str
    return int(raw) if raw.isdigit() else None


def verify_session_token(
    token: str | None, secret: str, max_age_sec: int = SESSION_MAX_AGE_SEC
) -> tuple[bool, int | None]:
    """Verifikasi signature HMAC + expiry. Return `(valid, user_id)`.

    `hmac.compare_digest` mencegah timing attack saat membandingkan signature.
    `max_age_sec` default ke absolute expiry (7 hari); middleware boleh mengoper
    nilai lebih kecil untuk enforce idle timeout (lihat docstring modul).
    Gagal parse/signature/expired → `(False, None)`. `user_id` bagian dari
    payload yang SUDAH diverifikasi signature-nya — aman dipercaya begitu
    `valid=True` (bukan diambil dari cookie terpisah yang bisa dipalsukan lepas).
    """
    parsed = _parse(token)
    if parsed is None:
        return False, None
    ts_str, uid_str, iat_str, sig = parsed
    if not ts_str.isdigit() or (iat_str and not iat_str.isdigit()):
        return False, None
    payload = f"{ts_str}.{uid_str}.{iat_str}" if iat_str else f"{ts_str}.{uid_str}"
    if not hmac.compare_digest(sig, _sign(payload, secret)):
        return False, None
    now = time.time()
    age = now - int(ts_str)
    if not (0 <= age <= max_age_sec):
        return False, None
    # Batas ABSOLUT sejak login — independen dari refresh idle timeout.
    login_age = now - int(iat_str or ts_str)
    if not (0 <= login_age <= SESSION_MAX_AGE_SEC):
        return False, None
    user_id = int(uid_str) if uid_str.isdigit() else None
    return True, user_id


def verify_login_token(candidate: str, secret: str) -> bool:
    """Bandingkan password yang diketik user vs OPENCLAWN_AUTH_TOKEN. Constant-time."""
    return hmac.compare_digest(candidate, secret)


def generate_csrf_token() -> str:
    """Token CSRF acak per sesi — disimpan di cookie terpisah + disuntik ke form."""
    return secrets.token_urlsafe(32)
