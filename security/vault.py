import base64
import hashlib
import os

from cryptography.fernet import Fernet


class Vault:
    """
    Credential tidak pernah masuk context/prompt — hanya diinjeksi saat outbound request.
    Jangan pernah log nilai dari vault.
    """

    def __init__(self):
        self._cache: dict[str, str] = {}

    async def get(self, key: str) -> str:
        if key in self._cache:
            return self._cache[key]
        value = os.environ.get(key)
        if not value:
            raise ValueError(f"Credential '{key}' tidak ditemukan di environment")
        self._cache[key] = value
        return value


# ── Enkripsi-at-rest untuk credential yang HARUS disimpan di DB ────────────────
# Beda dari Vault di atas (env-var-backed, satu key statis per nama per deploy):
# ini untuk data seperti mcp_servers.env — env var subprocess MCP per-baris yang
# user isi lewat API/UI, bukan dikenal saat deploy. CLAUDE.md §1.2 "credential
# tidak pernah masuk tabel DB" — dependency `cryptography` disetujui owner
# eksplisit untuk menutup gap ini (audit produksi 2026-07-28), lihat CLAUDE.md §7
# "Pengecualian sadar #4".

_FERNET_KEY_ENV = "OPENCLAWN_ENCRYPTION_KEY"


def _derive_fernet_key(raw: str) -> bytes:
    """Fernet butuh key 32-byte url-safe base64 — turunkan dari string apa pun
    di OPENCLAWN_ENCRYPTION_KEY (operator cukup generate string acak panjang,
    tak perlu tahu format Fernet)."""
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _get_fernet() -> Fernet:
    key = os.environ.get(_FERNET_KEY_ENV)
    if not key:
        raise ValueError(
            f"{_FERNET_KEY_ENV} tidak diset — wajib untuk menyimpan credential at-rest "
            "(mis. env var server MCP). Generate: "
            'python3 -c "import secrets; print(secrets.token_urlsafe(32))" lalu isi di .env.'
        )
    return Fernet(_derive_fernet_key(key))


def encrypt_secret(plaintext: str) -> str:
    """Enkripsi string untuk disimpan at-rest. Raises ValueError bila
    OPENCLAWN_ENCRYPTION_KEY belum diset — fail loud, jangan diam-diam
    simpan plaintext (CLAUDE.md §1.2)."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Dekripsi nilai dari encrypt_secret(). Raises ValueError (key tak diset)
    atau cryptography.fernet.InvalidToken (key salah / bukan ciphertext valid —
    mis. baris LAMA pra-enkripsi yang masih plaintext JSON). Caller yang perlu
    kompatibel dengan data lama harus tangkap keduanya dan fallback sendiri
    (lihat core/mcp_registry.py)."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
