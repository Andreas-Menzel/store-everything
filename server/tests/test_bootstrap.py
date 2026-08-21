"""Getting the first administrator into a fresh instance.

The circle this breaks — only an admin can create accounts, and a new instance has none —
is exactly the kind of thing that gets a "temporary" back door. These tests pin that there
isn't one: bootstrap works once, on an empty instance, and never again.
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import bootstrap, identity, passwords
from store_everything.app import create_app
from store_everything.db import alembic_config
from store_everything.tables import app_user, event
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _admins(database_url: str) -> list[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        select(app_user.c.email).where(app_user.c.role == "admin")
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()


async def test_it_creates_the_first_admin_and_audits_it(identity_database: str) -> None:
    """`identity_database` plus the app lifespan is the real first-run path."""
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            created = await bootstrap.create_first_admin(
                connection, email="First@Example.com ", password="a-long-enough-password"
            )
            await connection.commit()

            actions = (
                (await connection.execute(select(event.c.action, event.c.details))).mappings().all()
            )
    finally:
        await engine.dispose()

    assert created is not None
    # Normalized on the way in, like every other account.
    assert created.email == "first@example.com"
    assert created.role == "admin"

    bootstrap_events = [row for row in actions if row["details"].get("via") == "bootstrap"]
    assert len(bootstrap_events) == 1
    assert bootstrap_events[0]["action"] == "user.created"


async def test_it_refuses_once_any_account_exists(identity_database: str) -> None:
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            first = await bootstrap.create_first_admin(
                connection, email="first@example.com", password="a-long-enough-password"
            )
            await connection.commit()

            second = await bootstrap.create_first_admin(
                connection, email="second@example.com", password="a-long-enough-password"
            )
            await connection.commit()
    finally:
        await engine.dispose()

    assert first is not None
    assert second is None
    assert await _admins(identity_database) == ["first@example.com"]


async def test_a_member_account_also_blocks_bootstrap(identity_database: str) -> None:
    """ "Empty" means no accounts at all — not "no admins", which would be a back door."""
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await identity.create_user(
                connection,
                email="member@example.com",
                display_name="Member",
                password="a-long-enough-password",
                role="member",
                actor=identity.Actor.system(),
            )
            await connection.commit()

            created = await bootstrap.create_first_admin(
                connection, email="sneaky@example.com", password="a-long-enough-password"
            )
            await connection.commit()
    finally:
        await engine.dispose()

    assert created is None
    assert await _admins(identity_database) == []


async def test_a_weak_bootstrap_password_is_refused_before_any_row_is_written(
    identity_database: str,
) -> None:
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            with pytest.raises(passwords.WeakPasswordError):
                await bootstrap.create_first_admin(
                    connection, email="first@example.com", password="short"
                )
    finally:
        await engine.dispose()

    assert await _admins(identity_database) == []


async def test_startup_bootstrap_is_idempotent_across_restarts(identity_database: str) -> None:
    """Two starts with the same configuration leave one admin, not two."""
    settings = make_settings(
        database_url=identity_database,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="bootstrap-password-1",
    )

    for _ in range(2):
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            pass

    assert await _admins(identity_database) == ["admin@example.com"]


async def test_startup_survives_an_unmigrated_database(fresh_database: str) -> None:
    """A fresh install has no schema yet; start-up must not die over it (`/readyz` reports)."""
    settings = make_settings(
        database_url=fresh_database,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="bootstrap-password-1",
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        pass

    # And once the schema exists, the next start does the work.
    command.upgrade(alembic_config(fresh_database), "head")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        pass

    assert await _admins(fresh_database) == ["admin@example.com"]


async def test_startup_without_bootstrap_configuration_creates_nothing(
    identity_database: str,
) -> None:
    app = create_app(make_settings(database_url=identity_database))

    async with app.router.lifespan_context(app):
        pass

    assert await _admins(identity_database) == []
