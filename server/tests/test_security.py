"""Deny by default: nothing under `/api/v1` answers without authentication."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from store_everything.api.v1.router import API_V1_PREFIX, build_v1_router
from store_everything.app import create_app
from store_everything.problems import problem_type
from store_everything.security import require_auth
from tests.conftest import make_settings


def _api_routes(container: Any) -> Iterator[APIRoute]:
    """Every API route reachable from an app or router.

    `include_router` no longer flattens routes into `app.routes` — it stores a wrapper
    holding the original router — so enumeration has to recurse.
    """
    for route in getattr(container, "routes", []):
        if isinstance(route, APIRoute):
            yield route
        else:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from _api_routes(nested)


def _uses_dependency(dependant: Dependant, target: object) -> bool:
    return any(
        sub.call is target or _uses_dependency(sub, target) for sub in dependant.dependencies
    )


def _app_with_probe_route() -> FastAPI:
    """An app carrying an extra `/api/v1` route, standing in for future endpoints."""
    app = create_app(make_settings())
    router = build_v1_router(api_docs_enabled=False)

    @router.get("/probe")
    async def probe() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"reached": "yes"}

    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_v1_route_refuses_unauthenticated_calls() -> None:
    app = _app_with_probe_route()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"{API_V1_PREFIX}/probe")

    assert response.status_code == 401
    assert response.json()["type"] == problem_type("authentication-required")
    assert response.headers["www-authenticate"] == "Bearer"
    # The handler never ran.
    assert "reached" not in response.text


@pytest.mark.asyncio
async def test_a_bearer_token_is_still_refused() -> None:
    """No credential can be valid before identity exists — 401, never 200."""
    app = _app_with_probe_route()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"{API_V1_PREFIX}/probe",
            headers={"Authorization": "Bearer pretend-token"},
        )

    assert response.status_code == 401


def test_every_v1_route_carries_the_auth_dependency() -> None:
    """The structural guarantee: a route added without an auth decision stays closed."""
    app = _app_with_probe_route()

    v1_routes = [route for route in _api_routes(app) if route.path.startswith(API_V1_PREFIX)]

    assert v1_routes, "expected at least one /api/v1 route to check"
    for route in v1_routes:
        assert _uses_dependency(route.dependant, require_auth), route.path


def test_public_surface_is_exactly_the_documented_endpoints() -> None:
    """Any other unauthenticated endpoint is a spec bug (08-api-principles.md)."""
    app = create_app(make_settings())

    public = {
        route.path
        for route in _api_routes(app)
        if not _uses_dependency(route.dependant, require_auth)
    }

    assert public == {"/healthz", "/readyz"}


@pytest.mark.asyncio
async def test_openapi_schema_is_never_public(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API_V1_PREFIX}/openapi.json")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_builtin_docs_routes_are_not_mounted(client: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_schema_route_disappears_when_docs_are_disabled() -> None:
    app = create_app(make_settings(api_docs_enabled=False))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"{API_V1_PREFIX}/openapi.json")

    assert response.status_code == 404
