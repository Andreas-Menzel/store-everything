"""Helpers shared by the tagging tests.

Two things every one of them needs: an admin creating vocabulary through the real API (the
taxonomy is admin-only, so a test that inserted rows would not be testing the rule), and a
connection for the few states no API can reach yet — a machine-suggested tag, a rejection
record — because the code that handles them exists now and has to be held to it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from store_everything.api.v1.router import API_V1_PREFIX
from tests.identity_helpers import SAME_ORIGIN

TAGS = f"{API_V1_PREFIX}/tags"


def file_tags_url(file_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/files/{file_id}/tags"


def folder_tags_url(folder_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/folders/{folder_id}/tags"


async def create_tag(
    client: httpx.AsyncClient,
    name: str,
    *,
    parents: list[UUID | str] | None = None,
    aliases: list[str] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"name": name}
    if parents is not None:
        body["parents"] = [str(parent) for parent in parents]
    if aliases is not None:
        body["aliases"] = aliases
    return await client.post(TAGS, json=body, headers=SAME_ORIGIN)


async def added(client: httpx.AsyncClient, name: str, **kwargs: Any) -> UUID:
    """A created tag's id, asserting the creation worked — the common case in one line."""
    response = await create_tag(client, name, **kwargs)
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def update_tag(
    client: httpx.AsyncClient, tag_id: UUID | str, **changes: Any
) -> httpx.Response:
    return await client.patch(f"{TAGS}/{tag_id}", json=changes, headers=SAME_ORIGIN)


async def tag_file(
    client: httpx.AsyncClient,
    file_id: UUID | str,
    *,
    name: str | None = None,
    tag: UUID | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if tag is not None:
        body["tag"] = str(tag)
    return await client.post(file_tags_url(file_id), json=body, headers=SAME_ORIGIN)


async def tag_folder(
    client: httpx.AsyncClient,
    folder_id: UUID | str,
    *,
    name: str | None = None,
    tag: UUID | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if tag is not None:
        body["tag"] = str(tag)
    return await client.post(folder_tags_url(folder_id), json=body, headers=SAME_ORIGIN)


async def names_on_file(client: httpx.AsyncClient, file_id: UUID | str) -> list[str]:
    response = await client.get(file_tags_url(file_id))
    assert response.status_code == 200, response.text
    return [applied["name"] for applied in response.json()]


@asynccontextmanager
async def connected(database_url: str) -> AsyncGenerator[AsyncConnection]:
    """One connection, committed on the way out — for the states only a module can produce."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            yield connection
            await connection.commit()
    finally:
        await engine.dispose()
