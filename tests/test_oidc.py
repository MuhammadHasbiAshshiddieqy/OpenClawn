"""Test untuk security/oidc.py — OAuth2/OIDC login (TODO.md § Prioritas 5).

Security-critical: ID token verifikasi HARUS menolak signature/iss/aud/exp/nonce
yang tak cocok. Network (discovery/JWKS/token exchange) di-mock — test tak boleh
memanggil provider OIDC sungguhan.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from security.oidc import (
    OIDCError,
    build_authorize_url,
    exchange_code,
    generate_nonce,
    generate_state,
    verify_id_token,
    _discovery_cache,
    _jwks_cache,
)


def _fake_response(json_data: dict):
    """Respons httpx palsu: `.json()`/`.raise_for_status()` SINKRON (seperti httpx asli) —
    hanya `client.get()`/`client.post()` dan context manager client yang async."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _fake_async_client(get_side_effect=None, post_side_effect=None):
    """Client httpx.AsyncClient palsu: `async with` mengembalikan objek dengan
    `.get()`/`.post()` async (coroutine), tapi respons di dalamnya sinkron."""
    client_obj = MagicMock()

    async def _get(url, **kwargs):
        return get_side_effect(url, **kwargs)

    async def _post(url, **kwargs):
        return post_side_effect(url, **kwargs)

    client_obj.get = _get
    client_obj.post = _post

    ctx = MagicMock()

    async def _aenter(_self):
        return client_obj

    async def _aexit(_self, *args):
        return None

    ctx.__aenter__ = _aenter
    ctx.__aexit__ = _aexit
    return MagicMock(return_value=ctx)


ISSUER = "https://accounts.example.com"
CLIENT_ID = "test-client-id"

_TEST_KEY = RSAKey.generate_key(2048, private=True)


def _make_discovery_doc():
    return {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }


def _make_jwks_dict():
    public_key = RSAKey.import_key(_TEST_KEY.as_dict(private=False))
    return {"keys": [public_key.as_dict(kid="test-key-1")]}


def _make_id_token(claims_override: dict | None = None) -> str:
    header = {"alg": "RS256", "kid": "test-key-1"}
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-123",
        "email": "user@example.com",
        "name": "Test User",
        "exp": int(time.time()) + 3600,
        "nonce": "expected-nonce",
    }
    if claims_override:
        claims.update(claims_override)
    signing_key = RSAKey.import_key(
        _TEST_KEY.as_dict(private=True), parameters={"kid": "test-key-1"}
    )
    return jwt.encode(header, claims, signing_key)


@pytest.fixture(autouse=True)
def clear_caches():
    _discovery_cache.clear()
    _jwks_cache.clear()
    yield
    _discovery_cache.clear()
    _jwks_cache.clear()


def test_generate_state_and_nonce_are_random_and_url_safe():
    a, b = generate_state(), generate_state()
    assert a != b
    assert len(a) > 20
    n1, n2 = generate_nonce(), generate_nonce()
    assert n1 != n2
    assert len(n1) > 20


@pytest.mark.asyncio
async def test_build_authorize_url_includes_required_params():
    fake_client_cls = _fake_async_client(
        get_side_effect=lambda url, **kw: _fake_response(_make_discovery_doc())
    )

    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        url = await build_authorize_url(
            ISSUER, CLIENT_ID, "https://myapp.example.com/auth/callback", "state123", "nonce456"
        )

    assert url.startswith(f"{ISSUER}/authorize?")
    assert "client_id=test-client-id" in url
    assert "state=state123" in url
    assert "nonce=nonce456" in url
    assert "response_type=code" in url


@pytest.mark.asyncio
async def test_build_authorize_url_missing_endpoint_raises():
    fake_client_cls = _fake_async_client(get_side_effect=lambda url, **kw: _fake_response({}))

    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        with pytest.raises(OIDCError, match="authorization_endpoint"):
            await build_authorize_url(ISSUER, CLIENT_ID, "https://x/callback", "s", "n")


@pytest.mark.asyncio
async def test_exchange_code_returns_id_token():
    id_token = _make_id_token()
    fake_client_cls = _fake_async_client(
        get_side_effect=lambda url, **kw: _fake_response(_make_discovery_doc()),
        post_side_effect=lambda url, **kw: _fake_response(
            {"id_token": id_token, "access_token": "at-123"}
        ),
    )

    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        token = await exchange_code(ISSUER, CLIENT_ID, "secret", "https://x/callback", "code-abc")

    assert token == id_token


@pytest.mark.asyncio
async def test_exchange_code_missing_id_token_raises():
    fake_client_cls = _fake_async_client(
        get_side_effect=lambda url, **kw: _fake_response(_make_discovery_doc()),
        post_side_effect=lambda url, **kw: _fake_response({"access_token": "at-123"}),
    )

    with patch("security.oidc.httpx.AsyncClient", fake_client_cls):
        with pytest.raises(OIDCError, match="id_token"):
            await exchange_code(ISSUER, CLIENT_ID, "secret", "https://x/callback", "code-abc")


def _patch_discovery_and_jwks():
    def _get(url, **kwargs):
        if url.endswith("jwks"):
            return _fake_response(_make_jwks_dict())
        return _fake_response(_make_discovery_doc())

    return _fake_async_client(get_side_effect=_get)


@pytest.mark.asyncio
async def test_verify_id_token_valid_returns_claims():
    id_token = _make_id_token()
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        claims = await verify_id_token(ISSUER, CLIENT_ID, id_token, expected_nonce="expected-nonce")

    assert claims.subject == "user-123"
    assert claims.email == "user@example.com"
    assert claims.name == "Test User"


@pytest.mark.asyncio
async def test_verify_id_token_wrong_issuer_rejected():
    id_token = _make_id_token({"iss": "https://evil.example.com"})
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        with pytest.raises(OIDCError, match="iss"):
            await verify_id_token(ISSUER, CLIENT_ID, id_token, expected_nonce="expected-nonce")


@pytest.mark.asyncio
async def test_verify_id_token_wrong_audience_rejected():
    id_token = _make_id_token({"aud": "some-other-client"})
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        with pytest.raises(OIDCError, match="aud"):
            await verify_id_token(ISSUER, CLIENT_ID, id_token, expected_nonce="expected-nonce")


@pytest.mark.asyncio
async def test_verify_id_token_expired_rejected():
    id_token = _make_id_token({"exp": int(time.time()) - 100})
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        with pytest.raises(OIDCError, match="kedaluwarsa"):
            await verify_id_token(ISSUER, CLIENT_ID, id_token, expected_nonce="expected-nonce")


@pytest.mark.asyncio
async def test_verify_id_token_wrong_nonce_rejected():
    id_token = _make_id_token()
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        with pytest.raises(OIDCError, match="nonce"):
            await verify_id_token(ISSUER, CLIENT_ID, id_token, expected_nonce="wrong-nonce")


@pytest.mark.asyncio
async def test_verify_id_token_tampered_signature_rejected():
    id_token = _make_id_token()
    tampered = id_token[:-5] + "AAAAA"
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        with pytest.raises(OIDCError, match="signature"):
            await verify_id_token(ISSUER, CLIENT_ID, tampered, expected_nonce="expected-nonce")


@pytest.mark.asyncio
async def test_verify_id_token_missing_subject_rejected():
    id_token = _make_id_token({"sub": ""})
    with patch("security.oidc.httpx.AsyncClient", _patch_discovery_and_jwks()):
        with pytest.raises(OIDCError, match="sub"):
            await verify_id_token(ISSUER, CLIENT_ID, id_token, expected_nonce="expected-nonce")
