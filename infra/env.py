"""Pemuat `.env` minimal — tanpa dependency eksternal.

Stack sudah final (CLAUDE.md §7) dan kami tidak menambah `python-dotenv`.
Loader ini membaca file `.env` di root project dan mengisi `os.environ`
untuk key yang BELUM ada. Variabel environment asli selalu menang, sehingga
deploy/CI yang menyetel env langsung tidak ter-override oleh file `.env`.
"""

import os
from pathlib import Path

# Root project = dua tingkat di atas file ini (infra/env.py -> infra/ -> root).
_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: str | Path | None = None) -> None:
    """Muat pasangan KEY=VALUE dari file `.env` ke `os.environ` (idempoten).

    Diam saja bila file tidak ada — `.env` opsional. Key yang sudah ada di
    environment tidak ditimpa. Komentar (`#`) dan baris kosong diabaikan;
    tanda kutip pembungkus nilai di-strip.
    """
    env_path = Path(path) if path is not None else _DEFAULT_ENV_PATH
    if not env_path.is_file():
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value
