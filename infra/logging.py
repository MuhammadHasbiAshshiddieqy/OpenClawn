import structlog


def setup_logging() -> None:
    """Setup structlog JSON renderer. Dipanggil sekali saat startup."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


log = structlog.get_logger()
