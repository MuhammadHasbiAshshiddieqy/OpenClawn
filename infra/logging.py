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
]
# Nama field yang nilainya selalu di-redact penuh (apa pun isinya).
_SECRET_KEY_HINTS = ("api_key", "apikey", "token", "secret", "password", "authorization")
_REDACTED = "[REDACTED]"


def _scrub_value(value: str) -> str:
    for pat in _SECRET_VALUE_PATTERNS:
        value = pat.sub(_REDACTED, value)
    return value


def scrub_secrets(logger, method_name, event_dict: dict) -> dict:
    """structlog processor: redact secret di key sensitif & nilai berpola secret.

    Fail-soft: error apa pun saat scrub tidak boleh menjatuhkan logging.
    """
    try:
        for key, val in list(event_dict.items()):
            if any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
                event_dict[key] = _REDACTED
            elif isinstance(val, str):
                event_dict[key] = _scrub_value(val)
    except Exception:  # noqa: BLE001 — logging tak boleh gagal karena scrub
        pass
    return event_dict


def setup_logging() -> None:
    """Setup structlog JSON renderer. Dipanggil sekali saat startup."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            scrub_secrets,  # redact secret SEBELUM render (§1.2 defense-in-depth)
            structlog.processors.JSONRenderer(),
        ],
    )


log = structlog.get_logger()
