"""Every migration runs up **and** down against a real database.

The CI gate from 10-deployment-and-operations.md § upgrades: rollback is "redeploy the
previous image", which only works if `downgrade` is exercised, not merely written.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, create_engine, text

from store_everything.db import alembic_config, script_heads

BASELINE_EXTENSIONS = frozenset({"vector", "pg_trgm"})


def _installed_extensions(connection: Connection) -> frozenset[str]:
    rows = connection.execute(text("SELECT extname FROM pg_extension")).scalars().all()
    return frozenset(rows)


def _heads(connection: Connection) -> frozenset[str]:
    return frozenset(MigrationContext.configure(connection).get_current_heads())


@pytest.mark.integration
def test_migrations_run_up_and_down(fresh_database: str) -> None:
    config = alembic_config(fresh_database)
    engine = create_engine(fresh_database)

    try:
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert _installed_extensions(connection) >= BASELINE_EXTENSIONS
            assert _heads(connection) == script_heads()

        command.downgrade(config, "base")

        with engine.connect() as connection:
            assert not (BASELINE_EXTENSIONS & _installed_extensions(connection))
            assert _heads(connection) == frozenset()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_upgrade_is_repeatable(fresh_database: str) -> None:
    """A second run converges instead of failing — crash recovery re-runs migrations."""
    config = alembic_config(fresh_database)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine(fresh_database)
    try:
        with engine.connect() as connection:
            assert _heads(connection) == script_heads()
    finally:
        engine.dispose()


def test_exactly_one_migration_head_exists() -> None:
    """Branching migration heads make `head` ambiguous on upgrade."""
    assert len(script_heads()) == 1
