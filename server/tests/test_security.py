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
from store_everything.security import enforce_request_ceiling, require_auth
from tests.conftest import make_settings


def _api_routes(container: Any, inherited: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every API route reachable from an app or router, with the path a client would call.

    `include_router` no longer flattens routes into `app.routes` — it stores a wrapper
    holding the original router — so enumeration has to recurse. A router bakes *its own*
    prefix into the routes added directly to it, but not into the routes of a router it
    includes; hence `inherited`, which carries the ancestors' prefixes down. Getting this
    wrong makes a nested route silently invisible, and an invisible route is exactly what
    an auth-coverage test must not miss.
    """
    for route in getattr(container, "routes", []):
        if isinstance(route, APIRoute):
            yield inherited + route.path, route
            continue
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _api_routes(nested, inherited + getattr(container, "prefix", ""))


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
async def test_a_credential_less_request_never_reaches_the_database() -> None:
    """These settings point at a dead database, and the refusal is still `401`.

    That is the property worth pinning: authentication rejects a request carrying no
    credential before opening a connection, so a scanner cannot exhaust the pool and an
    instance with an unreachable database still answers honestly. Credentials that *are*
    presented are verified against a real database in `test_identity_api.py`.
    """
    app = _app_with_probe_route()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"{API_V1_PREFIX}/probe")

    assert response.status_code == 401


def test_every_v1_route_carries_the_auth_dependency() -> None:
    """The structural guarantee for routes added directly to the `/api/v1` router.

    Note the limit of introspection: a router included into another keeps its *original*
    routes, so dependencies inherited from the parent are invisible here. Effective
    behaviour is therefore asserted by request, in
    `test_every_documented_endpoint_refuses_unauthenticated_calls` — structure and
    behaviour together, because either alone can be fooled.
    """
    app = _app_with_probe_route()

    probe = [route for path, route in _api_routes(app) if path == f"{API_V1_PREFIX}/probe"]

    assert probe, "expected the probe route to be found"
    for route in probe:
        assert _uses_dependency(route.dependant, require_auth)
        assert _uses_dependency(route.dependant, enforce_request_ceiling)


def _operations(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) the published contract offers — the client's view of the API."""
    schema = app.openapi()
    return sorted(
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PATCH", "PUT", "DELETE"}
    )


#: Documented in 08-api-principles.md § endpoint map. Anything else answering without a
#: credential is a spec bug.
PUBLIC_OPERATIONS = {
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("POST", f"{API_V1_PREFIX}/auth/login"),
}

_PLACEHOLDER = "00000000-0000-7000-8000-000000000000"


def _concrete(path: str) -> str:
    for placeholder in ("{session_id}", "{token_id}", "{user_id}"):
        path = path.replace(placeholder, _PLACEHOLDER)
    return path


@pytest.mark.asyncio
async def test_every_documented_endpoint_refuses_unauthenticated_calls() -> None:
    """Behavioural coverage of the whole published surface, one request per operation.

    These settings point at an unreachable database, so a `401` also proves the refusal
    happened before any query — an endpoint that authenticated later would surface here as
    a `500`, and one that forgot to authenticate at all as a `2xx`.
    """
    app = create_app(make_settings())
    transport = httpx.ASGITransport(app=app)

    assert len(_operations(app)) > 10, "expected the identity surface to be published"

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for method, path in _operations(app):
            if (method, path) in PUBLIC_OPERATIONS:
                continue
            response = await client.request(method, _concrete(path))
            assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
            assert response.json()["type"] == problem_type("authentication-required")


@pytest.mark.asyncio
async def test_the_unauthenticated_surface_is_exactly_the_documented_one() -> None:
    """The complement of the test above: nothing else may answer without a credential."""
    app = create_app(make_settings())
    transport = httpx.ASGITransport(app=app)

    reachable: set[tuple[str, str]] = set()
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for method, path in _operations(app):
            response = await client.request(method, _concrete(path))
            if response.status_code != 401:
                reachable.add((method, path))

    assert reachable == PUBLIC_OPERATIONS


def test_the_public_login_route_is_rate_limited() -> None:
    """Its only shield is the ceiling, so losing that silently would be serious."""
    app = create_app(make_settings())

    login = [route for path, route in _api_routes(app) if path == f"{API_V1_PREFIX}/auth/login"]

    assert login, "expected the login route to exist"
    for route in login:
        assert _uses_dependency(route.dependant, enforce_request_ceiling)


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
