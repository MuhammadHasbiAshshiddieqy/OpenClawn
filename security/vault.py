import os


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
