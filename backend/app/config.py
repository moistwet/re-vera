"""Runtime configuration.

Every setting is read from the process environment (and from ``backend/.env``
when present) by pydantic-settings; field names map case-insensitively onto the
env var of the same name, so ``redis_url`` comes from ``REDIS_URL``. See
``backend/.env.example`` for the documented set.

No secret has a default here and none belongs in this file — milestone-2 keys
(``OPENAI_API_KEY`` and friends) join :class:`Settings` when the real pipeline
lands, and live only in the gitignored ``backend/.env``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend settings, loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    redis_url: str = "redis://localhost:6379/0"
    """Redis connection URL — cache, daily cap, and the per-job event stream."""

    allowed_extension_origin: str = "chrome-extension://*"
    """Single CORS origin allowed to call this API. Pin to the real extension id
    before shipping; the wildcard form exists because an unpacked extension's id
    is not known during development."""

    daily_cap: int = 20
    """Checks allowed per install ID per day (Asia/Singapore). A cost control as
    much as an abuse control — never bypassed outside local dev."""

    max_claims: int = 8
    """Maximum claims verified per article."""

    mock_step_delay: float = 0.85
    """Seconds between mock claim events in the milestone-1 fake pipeline."""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, built once and cached.

    Tests that need different values should call ``get_settings.cache_clear()``
    after patching the environment, or construct ``Settings(...)`` directly.
    """
    return Settings()
