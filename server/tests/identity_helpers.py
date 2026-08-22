"""Fixtures and helpers shared by the identity tests.

The app under test runs against a migrated database with exactly one bootstrapped admin,
which is the state a freshly installed instance is in — so these tests exercise the real
first-run path rather than a hand-inserted row.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.tables import event

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "bootstrap-password-1"

BASE_URL = "http://testserver"

#: What a browser on our own origin sends. Cookie-authenticated writes require it.
SAME_ORIGIN = {"Origin": BASE_URL}


async def login(
    client: httpx.AsyncClient, *, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
) -> httpx.Response:
    return await client.post(
        f"{API_V1_PREFIX}/auth/login",
        json={"email": email, "password": password},
        headers=SAME_ORIGIN,
    )


async def read_events(database_url: str, *, action: str | None = None) -> list[dict[str, Any]]:
    """Every event, oldest first — the audit trail as a test would read it."""
    query = select(
        event.c.action,
        event.c.actor_type,
        event.c.resource_type,
        event.c.resource_id,
        event.c.details,
        event.c.request_id,
        event.c.occurred_at,
    ).order_by(event.c.id)
    if action is not None:
        query = query.where(event.c.action == action)

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
    finally:
        await engine.dispose()
    return [dict(row) for row in rows]
