"""The migrations and `tables.py` must describe the same schema.

Migrations are hand-written (ADR-0012 leaves us with SQL, not an ORM that generates DDL),
so the table definitions the application queries through and the DDL the database actually
has are two artefacts that can drift. Drift is nasty: the code compiles, the tests that
mock nothing still pass locally, and production fails on a column that was never created.

Alembic's own comparison closes the gap — the same machinery `--autogenerate` uses, run as
an assertion instead of a code generator.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, create_engine

from store_everything.db import alembic_config
from store_everything.tables import metadata

#: Differences Alembic reports that are not drift. Kept deliberately tiny: every entry is
#: a thing this test can no longer catch.
_IGNORED_KINDS = frozenset({"add_table_comment", "remove_table_comment"})


def _differences(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True},
    )
    found = compare_metadata(context, metadata)
    return [
        difference
        for difference in found
        if not (isinstance(difference, tuple) and difference and difference[0] in _IGNORED_KINDS)
    ]


@pytest.mark.integration
def test_migrated_schema_matches_the_table_definitions(fresh_database: str) -> None:
    command.upgrade(alembic_config(fresh_database), "head")

    engine = create_engine(fresh_database)
    try:
        with engine.connect() as connection:
            differences = _differences(connection)
    finally:
        engine.dispose()

    assert differences == [], f"schema drift between migrations and tables.py: {differences}"


@pytest.mark.integration
def test_downgrade_leaves_no_identity_tables_behind(fresh_database: str) -> None:
    """A migration that cannot be undone is a migration that cannot be rolled back."""
    config = alembic_config(fresh_database)
    command.upgrade(config, "head")
    command.downgrade(config, "0001_baseline")

    engine = create_engine(fresh_database)
    try:
        with engine.connect() as connection:
            remaining = set(metadata.tables) & set(
                connection.dialect.get_table_names(connection)  # pyright: ignore[reportArgumentType]
            )
    finally:
        engine.dispose()

    assert remaining == set()
