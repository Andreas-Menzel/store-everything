"""Helpers shared by the workspace tests.

Two things every one of them needs: an instance configured for the storage layout under test
(the allow-list differs per test, so the app is built per test rather than by one fixture),
and a way to run the provisioning operation the way the worker would — claimed under a lease,
on its own connection, committing with the transition.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import operations, workspaces
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.app import create_app
from store_everything.config import Settings
from store_everything.runner import Job
from store_everything.tables import operation
from tests.identity_helpers import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    BASE_URL,
    SAME_ORIGIN,
    login,
)

MEMBER_EMAIL = "member@example.com"
MEMBER_PASSWORD = "member-password-1"

WORKSPACES = f"{API_V1_PREFIX}/workspaces"


@asynccontextmanager
async def instance(settings: Settings, **overrides: Any) -> AsyncGenerator[FastAPI]:
    """A running instance, its lifespan executed so the bootstrap admin really exists."""
    resolved = settings.model_copy(update=overrides) if overrides else settings
    app = create_app(resolved)
    async with app.router.lifespan_context(app):
        yield app


@asynccontextmanager
async def signed_in(
    app: FastAPI, *, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
) -> AsyncGenerator[httpx.AsyncClient]:
    """A client with a session, like one browser."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        response = await login(client, email=email, password=password)
        assert response.status_code == 200, response.text
        yield client


@asynccontextmanager
async def as_admin(settings: Settings, **overrides: Any) -> AsyncGenerator[httpx.AsyncClient]:
    """One instance with one signed-in administrator — the common case, in one context."""
    async with instance(settings, **overrides) as app, signed_in(app) as client:
        yield client


async def create_member(
    admin: httpx.AsyncClient, *, email: str = MEMBER_EMAIL, password: str = MEMBER_PASSWORD
) -> UUID:
    response = await admin.post(
        f"{API_V1_PREFIX}/users",
        json={"email": email, "display_name": "A Member", "password": password, "role": "member"},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def create_workspace(
    client: httpx.AsyncClient,
    name: str,
    *,
    adopt_path: Path | str | None = None,
    owner: UUID | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"name": name}
    if adopt_path is not None:
        body["adopt_path"] = str(adopt_path)
    if owner is not None:
        body["owner"] = str(owner)
    return await client.post(WORKSPACES, json=body, headers=SAME_ORIGIN)


async def provision_pending(database_url: str) -> list[dict[str, Any]]:
    """Run every queued `workspace.provision` exactly as the worker would.

    Deliberately not a call to the handler with a hand-made job: claiming counts the attempt
    and takes the lease, and the handler's writes are supposed to commit with the success
    transition. A test that skipped that would not be testing the thing that runs.
    """
    engine = create_async_engine(database_url)
    results: list[dict[str, Any]] = []
    try:
        async with engine.connect() as connection:
            while True:
                claimed = await operations.claim(
                    connection,
                    worker="test/provisioner",
                    lease=timedelta(minutes=5),
                    kinds=(workspaces.KIND,),
                )
                if claimed is None:
                    return results
                result = await workspaces.provision(Job(operation=claimed, connection=connection))
                await operations.succeed(connection, claimed=claimed, result=result)
                await connection.commit()
                results.append(result)
    finally:
        await engine.dispose()


async def provisioning_states(database_url: str, workspace_id: UUID) -> Sequence[str]:
    """The states of the provisioning operations for one workspace, oldest first."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(operation.c.state)
                    .where(
                        operation.c.kind == workspaces.KIND,
                        operation.c.subject_id == workspace_id,
                    )
                    .order_by(operation.c.created_at)
                )
            ).scalars()
            return list(rows)
    finally:
        await engine.dispose()


@asynccontextmanager
async def workspace_ready(
    settings: Settings, database_url: str, *, name: str = "Photos", **overrides: Any
) -> AsyncGenerator[tuple[httpx.AsyncClient, UUID, Path]]:
    """A signed-in owner and one **active** workspace: the state uploads need.

    Provisioning runs for real rather than being faked, so these tests exercise the directory
    and control area an upload actually writes into.
    """
    async with as_admin(settings, **overrides) as client:
        response = await create_workspace(client, name)
        assert response.status_code == 201, response.text
        body = response.json()
        await provision_pending(database_url)
        yield client, UUID(body["id"]), Path(body["root_path"])
