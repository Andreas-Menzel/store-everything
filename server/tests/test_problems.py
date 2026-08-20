"""The problem+json envelope (08-api-principles.md § errors)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from store_everything.problems import PROBLEM_MEDIA_TYPE, problem_type
from store_everything.schemas import BaseSchema
from tests.conftest import make_settings

from store_everything.app import create_app  # isort: skip


class _Payload(BaseSchema):
    count: int


def _app_with_probe_routes() -> FastAPI:
    app = create_app(make_settings())

    @app.post("/probe/validate")
    async def validate(payload: _Payload) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return {"count": payload.count}

    @app.get("/probe/boom")
    async def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("secret internal detail: connection string postgres://u:p@h/db")

    return app


@pytest.mark.asyncio
async def test_not_found_uses_the_problem_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)

    body = response.json()
    assert body["type"] == problem_type("not-found")
    assert body["title"] == "Not found"
    assert body["status"] == 404
    # `instance` is the bridge to the log line for this exact request.
    assert body["instance"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_method_not_allowed_uses_the_problem_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post("/healthz")

    assert response.status_code == 405
    assert response.json()["type"] == problem_type("method-not-allowed")


@pytest.mark.asyncio
async def test_validation_reports_every_field_without_echoing_values() -> None:
    app = _app_with_probe_routes()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/probe/validate",
            json={"count": "not-a-number", "colour": "sensitive-value"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == problem_type("validation")

    pointers = {error["pointer"] for error in body["errors"]}
    # All problems at once — the bad field *and* the unknown one.
    assert pointers == {"/body/count", "/body/colour"}
    # The submitted value is never reflected back.
    assert "sensitive-value" not in response.text
    assert "not-a-number" not in response.text


@pytest.mark.asyncio
async def test_unhandled_exception_leaks_nothing_and_keeps_the_request_id() -> None:
    app = _app_with_probe_routes()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/probe/boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)

    body = response.json()
    assert body["type"] == problem_type("internal")
    assert body["instance"] == response.headers["x-request-id"]

    # No stack trace, no dependency error strings, no connection details.
    assert "postgres://" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_error_responses_still_carry_security_headers() -> None:
    app = _app_with_probe_routes()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/probe/boom")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
