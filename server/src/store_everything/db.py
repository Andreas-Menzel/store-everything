"""Database engine and the two readiness facts: reachable, and schema current.

Migrations are **not** run at startup — when they run on a self-hosted upgrade is still
open (Q20). Until that is decided, `/readyz` reports the truth and `make migrate` applies
them, so a half-migrated instance can never quietly serve traffic.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from store_everything.config import Settings

MIGRATIONS_PATH = Path(__file__).parent / "migrations"

_URL_ATTRIBUTE = "sqlalchemy_url"


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,
        echo=False,
    )


def alembic_config(url: str | None = None) -> Config:
    """Programmatic Alembic config.

    The URL travels in `attributes`, never in the ini section: a password containing `%`
    would otherwise be mangled by ConfigParser interpolation.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_PATH))
    if url is not None:
        config.attributes[_URL_ATTRIBUTE] = url
    return config


def configured_url(config: Config) -> str | None:
    url = config.attributes.get(_URL_ATTRIBUTE)
    return url if isinstance(url, str) else None


def script_heads() -> frozenset[str]:
    """Revision heads the shipped code knows about."""
    directory = ScriptDirectory.from_config(alembic_config())
    return frozenset(directory.get_heads())


def _database_heads(connection: Connection) -> frozenset[str]:
    return frozenset(MigrationContext.configure(connection).get_current_heads())


async def ping(engine: AsyncEngine) -> None:
    """Raise if the database is unreachable."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def database_heads(engine: AsyncEngine) -> frozenset[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(_database_heads)


async def migrations_are_current(engine: AsyncEngine) -> bool:
    return await database_heads(engine) == script_heads()
