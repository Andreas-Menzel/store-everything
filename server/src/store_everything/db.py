"""Database engine and the two readiness facts: reachable, and schema current.

Migrations are **not** run at startup — when they run on a self-hosted upgrade is still
open (Q20). Until that is decided, `/readyz` reports the truth and `make migrate` applies
them, so a half-migrated instance can never quietly serve traffic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Depends, Request
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

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


async def request_connection(request: Request) -> AsyncIterator[AsyncConnection]:
    """One connection — and therefore one transaction — per request.

    Everything a *handler* touches shares it, which is what makes the event log a
    transactional outbox rather than a second write that can disagree (ADR-0007): the
    change and its event commit together or not at all. Authentication is the deliberate
    exception and runs on its own connection (`security.require_auth`).

    SQLAlchemy begins the transaction implicitly on the first statement, so the commit
    here is what makes it durable. A handler that must persist something *and then* fail
    the request — recording a failed login before answering `401` — commits explicitly
    before raising; see `api/v1/auth.py`.

    **When** that commit runs is part of the guarantee, not an implementation detail: see
    `DatabaseConnection`.
    """
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as connection:
        try:
            yield connection
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()


DatabaseConnection = Annotated[AsyncConnection, Depends(request_connection, scope="function")]
"""The connection a handler and everything it calls share within one request.

`scope="function"` is load-bearing. A request-scoped dependency with `yield` is torn down
*after* the response has been sent, so a commit that failed there could no longer change the
`2xx` the client was already holding: a false success over rolled-back rows — sharpest where
the transaction defers a constraint to `COMMIT` (the cross-workspace move in
`api/v1/folders.py`), and where the bytes already moved on disk. Function scope ends the
dependency when the handler returns and *before* the response starts, so a failed commit is
answered as `500` — "never `200` with an error body"
(08-api-principles.md § errors, 12-reliability.md § the request transaction).

It also stops a streamed download from holding a pooled connection for the whole transfer.

Needs FastAPI >= 0.121, where `scope` was introduced; `tests/test_request_lifecycle.py`
asserts the ordering rather than trusting the version pin to keep it.
"""
