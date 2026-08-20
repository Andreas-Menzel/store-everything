"""Deployment configuration, read from the environment.

12-factor: nothing here is hardcoded or committed; secrets are held in secret types and
never logged (10-deployment-and-operations.md § configuration & secrets).
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_ASYNC_DRIVER = "postgresql+psycopg"


class Settings(BaseSettings):
    """Every deployment knob, prefixed `SE_` so it cannot collide with unrelated variables."""

    model_config = SettingsConfigDict(
        env_prefix="SE_",
        env_file=".env",
        env_file_encoding="utf-8",
        # The compose `.env` also carries variables meant for other containers; ignore them
        # rather than failing to start.
        extra="ignore",
    )

    app_env: Literal["development", "production"] = "production"
    log_level: LogLevel = "INFO"

    database_url: SecretStr
    """PostgreSQL DSN. Required — the service must not invent a default datastore."""

    api_docs_enabled: bool = True
    """Serve the OpenAPI schema to authenticated users (08-api-principles.md)."""

    cors_allow_origins: tuple[str, ...] = ()
    """Deny by default: an empty list installs no CORS middleware at all."""

    host: str = "127.0.0.1"
    """Bind address. Containers set `SE_HOST=0.0.0.0`; the safe default stays loopback."""

    port: int = 8000

    forwarded_allow_ips: str = ""
    """Proxy addresses whose `X-Forwarded-*` headers are trusted. Empty = trust nobody.

    A spoofed client IP would poison rate limiting and audit records
    (ADR-0009, 10-deployment-and-operations.md), so this is opt-in per deployment.
    """

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Accept `a,b` in the environment.

        Pydantic would otherwise demand JSON for a sequence field, so the obvious
        `SE_CORS_ALLOW_ORIGINS=` (empty, meaning "none") would crash the service at boot.
        """
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @property
    def sqlalchemy_url(self) -> str:
        """The DSN with the driver this service actually uses (ADR-0012: psycopg 3)."""
        url = self.database_url.get_secret_value()
        scheme, separator, rest = url.partition("://")
        if not separator:
            return url
        if scheme in {"postgresql", "postgres"}:
            return f"{_ASYNC_DRIVER}://{rest}"
        return url

    @property
    def trust_proxy_headers(self) -> bool:
        return bool(self.forwarded_allow_ips.strip())


def load_settings() -> Settings:
    """Read settings from the environment.

    The ignore is structural: pydantic-settings populates required fields from the
    environment, which a static type checker cannot see.
    """
    return Settings()  # pyright: ignore[reportCallIssue]
