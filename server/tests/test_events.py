"""The event log's own guarantees, independent of any feature that writes to it.

ADR-0007 makes one promise that everything else leans on: an event exists exactly when the
change it describes does. That is a property of *how* it is written — same connection, same
transaction — so it is worth testing directly rather than only through the endpoints.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import events, identity
from store_everything.events import Actor
from store_everything.tables import app_user, event

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _count(connection: object, table: object) -> int:
    from sqlalchemy.ext.asyncio import AsyncConnection

    assert isinstance(connection, AsyncConnection)
    return (await connection.execute(select(func.count()).select_from(table))).scalar_one()  # pyright: ignore[reportArgumentType]


async def test_a_rolled_back_change_leaves_no_event(identity_database: str) -> None:
    """F-011/FR-4, from the failing side: no phantom entries."""
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await identity.create_user(
                connection,
                email="ghost@example.com",
                display_name="Ghost",
                password="a-long-enough-password",
                role="member",
                actor=Actor.system(),
            )
            # Both the row and its event are pending in the same transaction.
            assert await _count(connection, app_user) == 1
            assert await _count(connection, event) == 1

            await connection.rollback()

            assert await _count(connection, app_user) == 0
            assert await _count(connection, event) == 0
    finally:
        await engine.dispose()


async def test_a_committed_change_always_leaves_one_event(identity_database: str) -> None:
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            user = await identity.create_user(
                connection,
                email="kept@example.com",
                display_name="Kept",
                password="a-long-enough-password",
                role="member",
                actor=Actor.system(),
            )
            await connection.commit()

            rows = (
                (
                    await connection.execute(
                        select(event.c.action, event.c.resource_id, event.c.actor_type)
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0]["action"] == events.USER_CREATED
    assert rows[0]["resource_id"] == user.id
    assert rows[0]["actor_type"] == "system"


@pytest.mark.parametrize(
    "key",
    ["password", "Password", "api_token", "client_secret", "credentials", "PASSWORD_HASH"],
)
async def test_a_credential_shaped_detail_key_is_refused(identity_database: str, key: str) -> None:
    """The log is the one table nothing deletes, so a secret written here is permanent."""
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            with pytest.raises(events.UnsafeEventDetailsError):
                await events.record(
                    connection,
                    action="test.attempted",
                    resource_type="user",
                    actor=Actor.system(),
                    details={key: "hunter2"},
                )
    finally:
        await engine.dispose()


async def test_ordinary_details_are_accepted(identity_database: str) -> None:
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await events.record(
                connection,
                action="test.recorded",
                resource_type="user",
                actor=Actor.system(),
                details={"email": "someone@example.com", "fields": ["display_name"]},
            )
            await connection.commit()

            stored = (await connection.execute(select(event.c.details))).scalar_one()
    finally:
        await engine.dispose()

    assert stored == {"email": "someone@example.com", "fields": ["display_name"]}


async def test_a_user_actor_must_be_identified(identity_database: str) -> None:
    """An audit record that says "a user did it" without saying which is not an audit record."""
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                await events.record(
                    connection,
                    action="test.attempted",
                    resource_type="user",
                    actor=Actor("user", None),
                )
    finally:
        await engine.dispose()
