"""Readiness is honest: ready means reachable database *and* current schema."""

from __future__ import annotations

import logging

import httpx
import pytest
from alembic import command

from store_everything.app import create_app
from store_everything.db import alembic_config
from store_everything.log import configure_logging
from store_everything.problems import problem_type
from tests.conftest import make_settings


async def _get_readyz(database_url: str) -> httpx.Response:
    app = create_app(make_settings(database_url=database_url))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/readyz")


@pytest.mark.asyncio
async def test_not_ready_when_the_database_is_unreachable(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["type"] == problem_type("service-not-ready")
    assert body["instance"] == response.headers["x-request-id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_not_ready_while_migrations_are_pending(fresh_database: str) -> None:
    response = await _get_readyz(fresh_database)

    assert response.status_code == 503
    assert "migrations" in response.json()["detail"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ready_once_the_schema_is_current(fresh_database: str) -> None:
    command.upgrade(alembic_config(fresh_database), "head")

    response = await _get_readyz(fresh_database)

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_probing_readiness_is_quiet(
    fresh_database: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A probe runs every few seconds; it must not narrate itself into the log.

    Reading the schema version drives Alembic, which logs at INFO by default.
    """
    command.upgrade(alembic_config(fresh_database), "head")
    configure_logging("INFO")

    with caplog.at_level(logging.INFO):
        await _get_readyz(fresh_database)

    noisy = [record for record in caplog.records if record.name.startswith("alembic")]
    assert noisy == []
