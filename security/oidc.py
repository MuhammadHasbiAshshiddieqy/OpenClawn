"""OAuth2/OIDC login (TODO.md § Prioritas 5) — mode auth TAMBAHAN di samping
shared-secret (`security/auth.py`), bukan penggantinya. Operator pilih SATU
provider generik yang kompatibel Google/Microsoft/Okta/dsb via discovery
document standar (`{issuer}/.well-known/openid-configuration`) — bukan
integrasi vendor-spesifik.

Alur (Authorization Code + state/nonce, tanpa PKCE — client_secret confidential
client sudah cukup untuk server-side self-host, PKCE penting untuk public client
seperti SPA/mobile yang tak bisa simpan secret):
1. `build_authorize_url()` — redirect user ke provider dengan `state` (anti-CSRF)
   dan `nonce` (anti-replay ID token) acak, disimpan di cookie sementara.
2. Provider redirect balik ke `/auth/callback?code=...&state=...`.
3. `exchange_code()` — tukar `code` → `id_token`+`access_token` (network, POST
   ke token_endpoint).
4. `verify_id_token()` — verifikasi signature (JWKS provider) + klaim standar
   (iss/aud/exp/nonce) sebelum dipercaya. Gagal di titik manapun → login ditolak,
   BUKAN fail-open (beda dari auth_token kosong yang sengaja fail-open).

Setelah verifikasi sukses, sesi yang diterbitkan SAMA PERSIS dengan shared-secret
(`create_session_token`, cookie `openclawn_session`) — OIDC hanya mengganti CARA
membuktikan identitas di titik login, bukan mekanisme sesi setelahnya. Tetap
single-user secara internal (§7): OIDC memverifikasi SIAPA yang login, bukan
membuka multi-akun/RBAC (itu Prioritas 5 sub-item terpisah).

Discovery document & JWKS provider di-cache in-process (TTL) — dipanggil di
JALUR LOGIN (jarang dibanding tiap request), bukan di middleware tiap request.
"""

import time
import secrets
from dataclasses import dataclass

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet

DISCOVERY_CACHE_TTL_SEC = 3600
_discovery_cache: dict[str, tuple[float, dict]] = {}
_jwks_cache: dict[str, tuple[float, KeySet]] = {}


class OIDCError(Exception):
    """Kegagalan di jalur OIDC (discovery/exchange/verifikasi) — pesan jelas
    untuk log operator, BUKAN fail-open. Login ditolak, bukan diloloskan."""


@dataclass(frozen=True)
class OIDCClaims:
    """Klaim ID token yang relevan setelah verifikasi berhasil."""

    subject: str
    email: str | None
    name: str | None


def generate_state() -> str:
    """Token anti-CSRF untuk alur redirect — dibandingkan saat callback."""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    """Token anti-replay untuk ID token — provider menyertakannya balik di klaim `nonce`."""
    return secrets.token_urlsafe(32)


async def _get_discovery(issuer: str) -> dict:
    """Ambil `.well-known/openid-configuration`, cache in-process (TTL 1 jam).

    Dipanggil di jalur login (redirect awal + callback) — jarang dibanding
    request biasa, tapi tetap di-cache agar tak network-call tiap login.
    """
    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached and now - cached[0] < DISCOVERY_CACHE_TTL_SEC:
        return cached[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            doc = resp.json()
    except httpx.HTTPError as e:
        raise OIDCError(f"gagal ambil discovery document dari {url}: {e}") from e
    _discovery_cache[issuer] = (now, doc)
    return doc


async def _get_jwks(jwks_uri: str) -> KeySet:
    """Ambil JWKS provider (public key untuk verifikasi signature ID token), cache TTL 1 jam."""
    now = time.monotonic()
    cached = _jwks_cache.get(jwks_uri)
    if cached and now - cached[0] < DISCOVERY_CACHE_TTL_SEC:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            jwks_dict = resp.json()
    except httpx.HTTPError as e:
        raise OIDCError(f"gagal ambil JWKS dari {jwks_uri}: {e}") from e
    keyset = KeySet.import_key_set(jwks_dict)
    _jwks_cache[jwks_uri] = (now, keyset)
    return keyset


async def build_authorize_url(
    issuer: str, client_id: str, redirect_uri: str, state: str, nonce: str
) -> str:
    """Bangun URL redirect ke provider (authorization_endpoint dari discovery doc)."""
    doc = await _get_discovery(issuer)
    endpoint = doc.get("authorization_endpoint")
    if not endpoint:
        raise OIDCError(f"discovery document {issuer} tak punya authorization_endpoint")
    params = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
        }
    )
    return f"{endpoint}?{params}"


async def exchange_code(
    issuer: str, client_id: str, client_secret: str, redirect_uri: str, code: str
) -> str:
    """Tukar authorization code → id_token mentah (belum diverifikasi) via token_endpoint.

    Return id_token string (JWT compact form). Caller HARUS memanggil
    `verify_id_token()` sebelum mempercayai isinya — token mentah dari network
    tidak boleh dipercaya tanpa verifikasi signature.
    """
    doc = await _get_discovery(issuer)
    endpoint = doc.get("token_endpoint")
    if not endpoint:
        raise OIDCError(f"discovery document {issuer} tak punya token_endpoint")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            resp.raise_for_status()
            token_response = resp.json()
    except httpx.HTTPError as e:
        raise OIDCError(f"gagal tukar authorization code: {e}") from e
    id_token = token_response.get("id_token")
    if not id_token:
        raise OIDCError("response token_endpoint tak menyertakan id_token")
    return id_token


async def verify_id_token(
    issuer: str, client_id: str, id_token: str, expected_nonce: str
) -> OIDCClaims:
    """Verifikasi signature (JWKS provider) + klaim standar (iss/aud/exp/nonce).

    Gagal di titik manapun → `OIDCError`, login ditolak. TIDAK fail-open — beda
    dari `auth_token` kosong yang sengaja membolehkan akses tanpa login (§ desain
    lama, opt-in). OIDC yang SUDAH dikonfigurasi harus verifikasi ketat.
    """
    doc = await _get_discovery(issuer)
    jwks_uri = doc.get("jwks_uri")
    if not jwks_uri:
        raise OIDCError(f"discovery document {issuer} tak punya jwks_uri")
    keyset = await _get_jwks(jwks_uri)

    try:
        token = jwt.decode(id_token, keyset, algorithms=["RS256", "ES256"])
    except Exception as e:  # noqa: BLE001 — signature/format invalid, tolak apa pun errornya
        raise OIDCError(f"verifikasi signature ID token gagal: {e}") from e

    claims = token.claims
    if claims.get("iss", "").rstrip("/") != issuer.rstrip("/"):
        raise OIDCError("klaim 'iss' tak cocok dengan issuer terkonfigurasi")
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if client_id not in aud_list:
        raise OIDCError("klaim 'aud' tak menyertakan client_id kita")
    exp = claims.get("exp")
    if not exp or time.time() > exp:
        raise OIDCError("ID token kedaluwarsa")
    if claims.get("nonce") != expected_nonce:
        raise OIDCError("klaim 'nonce' tak cocok — kemungkinan replay attack")

    subject = claims.get("sub")
    if not subject:
        raise OIDCError("ID token tak punya klaim 'sub'")
    return OIDCClaims(subject=subject, email=claims.get("email"), name=claims.get("name"))
