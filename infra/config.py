from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class AppConfig:
    """Semua konfigurasi global. frozen=True agar tidak bisa diubah setelah init."""

    db_path: str = "data/openclawn.db"
    ollama_base: str = "http://localhost:11434"
    anthropic_base: str = "https://api.anthropic.com"
    max_context_tokens: int = 28_000
    max_tool_hops: int = 5
    llm_max_retries: int = 3
    approval_timeout_sec: int = 120
    decay_interval_sec: int = 3600
    skill_decay_base: float = 0.97
    skill_archive_threshold: float = 0.3
    skill_revive_boost: float = 0.5
    max_active_skills: int = 8
    confidence_threshold: int = 4
    # fallback chain: urutan model jika provider utama gagal
    fallback_chain: tuple = field(
        default_factory=lambda: (
            ("ollama", "gemma4:12b"),
            ("ollama", "gemma4:e4b"),
            ("ollama", "gemma4:e2b"),
            ("anthropic", "claude-haiku-4-5-20251001"),
        )
    )

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            db_path=os.environ.get("OPENCLAWN_DB", "data/openclawn.db"),
            ollama_base=os.environ.get("OLLAMA_BASE", "http://localhost:11434"),
            anthropic_base=os.environ.get("ANTHROPIC_BASE", "https://api.anthropic.com"),
        )


# Singleton global — di-inject ke semua modul via dependency injection
CONFIG = AppConfig.from_env()
