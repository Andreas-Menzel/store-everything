"""Application wiring: lifecycle, CORS policy, and the generated schema."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from store_everything.api.v1.router import openapi_schema
from store_everything.app import create_app
from tests.conftest import make_settings

ALLOWED_ORIGIN = "https://app.example"


@pytest.mark.asyncio
async def test_lifespan_releases_the_connection_pool() -> None:
    app = create_app(make_settings())

    async with app.router.lifespan_context(app):
        pool_while_running = app.state.engine.pool

    # dispose() replaces the pool, so a new identity proves shutdown ran.
    assert app.state.engine.pool is not pool_while_running


@pytest.mark.asyncio
async def test_cors_is_denied_by_default(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"Origin": ALLOWED_ORIGIN})

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_cors_allows_only_configured_origins() -> None:
    app = create_app(make_settings(cors_allow_origins=(ALLOWED_ORIGIN,)))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.get("/healthz", headers={"Origin": ALLOWED_ORIGIN})
        rejected = await client.get("/healthz", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "access-control-allow-origin" not in rejected.headers


def test_openapi_schema_can_be_generated() -> None:
    """Guards the export the generated client is built from (08-api-principles.md)."""
    schema = create_app(make_settings()).openapi()

    assert schema["info"]["title"] == "Store Everything"
    assert "/healthz" in schema["paths"]
    assert "/api/v1/openapi.json" in schema["paths"]


@pytest.mark.asyncio
async def test_schema_endpoint_serves_the_generated_schema() -> None:
    app = create_app(make_settings())
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "app": app,
    }

    response = await openapi_schema(Request(scope))

    assert json.loads(bytes(response.body))["info"]["title"] == "Store Everything"
