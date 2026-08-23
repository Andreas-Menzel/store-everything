"""Helpers for the extraction tests: an installed extractor, and a client that speaks as one.

Every test here needs the same two things — an extractor an administrator has provisioned and
credentialed, and a container-shaped client holding that credential — so they are assembled once
rather than in each test.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything.api.extractor_api import EXTRACTOR_API_PREFIX
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.tables import extraction_run, operation
from tests.identity_helpers import BASE_URL, SAME_ORIGIN
from tests.workspace_helpers import create_workspace, instance, provision_pending, signed_in

EXTRACTORS = f"{API_V1_PREFIX}/extractors"
CLAIM = f"{EXTRACTOR_API_PREFIX}/jobs/claim"
REGISTRATION = f"{EXTRACTOR_API_PREFIX}/registration"


def manifest(extractor_id: str = "pdf-text", **overrides: Any) -> dict[str, Any]:
    """A manifest that will be routed real work: it accepts something and produces something."""
    document: dict[str, Any] = {
        "id": extractor_id,
        "version": "1.0.0",
        "api_version": "v1",
        "model": {"name": "pymupdf", "version": "1.28"},
        "accepts": {"mime_types": ["*/*"]},
        "produces": ["text_segments"],
    }
    document.update(overrides)
    return document


@dataclass(frozen=True, slots=True)
class Installed:
    """An extractor that exists, has registered, and has a credential to work with."""

    id: str
    token: str


async def install(
    app: FastAPI,
    admin: httpx.AsyncClient,
    extractor_id: str = "pdf-text",
    **manifest_overrides: Any,
) -> Installed:
    """Provision, credential and register one extractor — the whole of chunk 2 in one call."""
    provisioned = await admin.post(EXTRACTORS, json={"id": extractor_id}, headers=SAME_ORIGIN)
    assert provisioned.status_code == 201, provisioned.text
    token = provisioned.json()["token"]

    async with as_extractor(app, token) as extractor:
        registered = await extractor.put(
            REGISTRATION, json=manifest(extractor_id, **manifest_overrides)
        )
    assert registered.status_code == 200, registered.text
    return Installed(id=extractor_id, token=token)


@asynccontextmanager
async def as_extractor(app: FastAPI, token: str) -> AsyncGenerator[httpx.AsyncClient]:
    """A client presenting an extractor credential and nothing else — like a container.

    Deliberately its own client rather than the administrator's with a header swapped: a
    container has no session cookie, and a test that lent it one would not be testing the
    credential separation the rest of this suite asserts.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


@asynccontextmanager
async def extraction_ready(
    settings: Settings, database_url: str, *, name: str = "Papers"
) -> AsyncGenerator[tuple[FastAPI, httpx.AsyncClient, UUID, Path]]:
    """An instance with an owner, one active workspace, and the app itself in hand.

    Like `workspace_ready`, plus the application object: these tests need to build a *second*
    client that authenticates as an extractor rather than as a person.
    """
    async with instance(settings) as app, signed_in(app) as client:
        created = await create_workspace(client, name)
        assert created.status_code == 201, created.text
        await provision_pending(database_url)
        yield app, client, UUID(created.json()["id"]), Path(created.json()["root_path"])


async def claim_one(extractor: httpx.AsyncClient) -> dict[str, Any] | None:
    """Claim without waiting. `None` is the `204` an idle queue answers with."""
    response = await extractor.post(CLAIM, json={})
    if response.status_code == 204:
        return None
    assert response.status_code == 200, response.text
    return response.json()


async def finish(
    extractor: httpx.AsyncClient, job: dict[str, Any], **outputs: Any
) -> httpx.Response:
    """Submit a result. Extra keyword arguments are the envelope's outputs."""
    return await extractor.post(
        f"{EXTRACTOR_API_PREFIX}/jobs/{job['id']}/result",
        json={"attempt": job["attempt"], **outputs},
    )


async def stage(
    extractor: httpx.AsyncClient, job: dict[str, Any], data: bytes
) -> tuple[str, httpx.Response]:
    """Stage one derived asset, the way the two-phase result works. Returns its digest."""
    digest = hashlib.sha256(data).hexdigest()
    response = await extractor.put(
        f"{EXTRACTOR_API_PREFIX}/jobs/{job['id']}/assets/{digest}",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    return digest, response


async def rows_in(database_url: str, table: Any, *columns: str) -> list[dict[str, Any]]:
    """Whatever one of the result tables holds, as a test would read it."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            selected = [table.c[name] for name in columns] if columns else list(table.c)
            rows = (await connection.execute(select(*selected))).mappings()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


async def report_error(
    extractor: httpx.AsyncClient, job: dict[str, Any], *, message: str, retryable: bool = True
) -> httpx.Response:
    return await extractor.post(
        f"{EXTRACTOR_API_PREFIX}/jobs/{job['id']}/error",
        json={"attempt": job["attempt"], "message": message, "retryable": retryable},
    )


async def heartbeat(extractor: httpx.AsyncClient, job: dict[str, Any]) -> httpx.Response:
    return await extractor.post(
        f"{EXTRACTOR_API_PREFIX}/jobs/{job['id']}/heartbeat", json={"attempt": job["attempt"]}
    )


async def read_input(
    extractor: httpx.AsyncClient, job: dict[str, Any], index: int = 0, **kwargs: Any
) -> httpx.Response:
    return await extractor.get(f"{EXTRACTOR_API_PREFIX}/jobs/{job['id']}/inputs/{index}", **kwargs)


async def runs_in(database_url: str) -> list[dict[str, Any]]:
    """Every extraction run, oldest first, as a test would read the table."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        extraction_run.c.id,
                        extraction_run.c.extractor_id,
                        extraction_run.c.file_version_id,
                        extraction_run.c.state,
                        extraction_run.c.generation,
                        extraction_run.c.extractor_version,
                        extraction_run.c.model_version,
                        extraction_run.c.error,
                    ).order_by(extraction_run.c.created_at, extraction_run.c.extractor_id)
                )
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


async def jobs_in(database_url: str) -> list[dict[str, Any]]:
    """Every extraction *operation* — the queue side, where priority and attempts live."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        operation.c.id,
                        operation.c.kind,
                        operation.c.state,
                        operation.c.priority,
                        operation.c.attempt,
                        operation.c.cancel_requested,
                        operation.c.idempotency_key,
                    )
                    .where(operation.c.kind.like("extract.%"))
                    .order_by(operation.c.created_at)
                )
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


async def expire_lease(database_url: str, job_id: UUID | str) -> None:
    """Make one job's lease lapse, which is what a killed worker leaves behind."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                update(operation)
                .where(operation.c.id == UUID(str(job_id)))
                .values(lease_expires_at=text("now() - interval '1 hour'"))
            )
            await connection.commit()
    finally:
        await engine.dispose()
