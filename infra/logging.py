import re

import structlog

# Defense-in-depth §1.2: Vault menjaga credential keluar dari prompt/context, TAPI
# sebuah API key bisa tak sengaja masuk log lewat string exception atau field event.
# Processor di bawah me-redact nilai yang menyerupai secret SEBELUM di-render JSON.
# Ini lapisan terakhir, bukan izin untuk log secret — tetap jangan log nilai vault.

# Pola nilai yang dianggap secret (redact seluruh match).
_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI/Anthropic-style
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),  # Authorization: Bearer ...
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub token
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),  # Google API key
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack token
    # Audit 2026-09-26: Tavily & GitHub fine-grained sebelumnya lolos.
    re.compile(r"tvly-[A-Za-z0-9_-]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
]
# Nama field yang nilainya selalu di-redact penuh (apa pun isinya). Audit
# 2026-09-26: dicocokkan per SEGMEN kata (dipisah non-alfanumerik), bukan
# substring — sebelumnya "token" membuat tokens_in/max_tokens/input_tokens ikut
# [REDACTED] (metrik penggunaan hilang dari log). "access_token", "api_key",
# "client_secret", "Authorization" tetap ter-redact.
_SECRET_KEY_SEGMENTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "apikey",
        "credential",
        "credentials",
        "cookie",
    }
)
_SECRET_KEY_PHRASES = ("api_key", "private_key", "access_key")
_REDACTED = "[REDACTED]"


def _is_secret_key(key) -> bool:
    k = str(key).lower()
    if any(p in k for p in _SECRET_KEY_PHRASES):
        return True
    return any(seg in _SECRET_KEY_SEGMENTS for seg in re.split(r"[^a-z0-9]+", k))


def _scrub_value(value: str) -> str:
    for pat in _SECRET_VALUE_PATTERNS:
        value = pat.sub(_REDACTED, value)
    return value


def _scrub_container(value):
    """Rekursif ke dict/list/tuple — audit produksi 2026-07-28: sebelumnya hanya
    string di TOP-LEVEL event_dict yang di-scrub; sebuah field seperti
    `headers={"Authorization": "Bearer ..."}` lolos utuh karena key hint & pola
    secret cuma dicek satu tingkat, dan value-nya dict (bukan str) jadi tak
    disentuh sama sekali."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_secret_key(k) else _scrub_container(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_container(v) for v in value)
    if isinstance(value, str):
        return _scrub_value(value)
    return value


def scrub_secrets(logger, method_name, event_dict: dict) -> dict:
    """structlog processor: redact secret di key sensitif & nilai berpola secret.

    Rekursif ke dict/list bersarang (lihat `_scrub_container`), bukan cuma
    field top-level. Fail-soft: error apa pun saat scrub tidak boleh
    menjatuhkan logging.
    """
    try:
        for key, val in list(event_dict.items()):
            if _is_secret_key(key):
                event_dict[key] = _REDACTED
            else:
                event_dict[key] = _scrub_container(val)
    except Exception:  # noqa: BLE001 — logging tak boleh gagal karena scrub
        pass
    return event_dict


def setup_logging() -> None:
    """Setup structlog JSON renderer. Dipanggil sekali saat startup (idempoten)."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            scrub_secrets,  # redact secret SEBELUM render (§1.2 defense-in-depth)
            structlog.processors.JSONRenderer(),
        ],
    )


# Audit 2026-09-26: pasang konfigurasi (dengan scrubber) SAAT modul diimpor.
# Sebelumnya hanya lifespan web yang memanggil setup_logging() — skrip CLI
# (scripts/*.py) yang mengimpor modul core berjalan dengan konfigurasi default
# structlog TANPA scrub_secrets.
setup_logging()

log = structlog.get_logger()
