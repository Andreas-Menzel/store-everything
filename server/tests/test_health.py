"""Liveness endpoint and the response invariants every response must satisfy."""

from __future__ import annotations

import re

import httpx
import pytest

REQUEST_ID_PATTERN = re.compile(r"^req_[0-9a-f]{32}$")


@pytest.mark.asyncio
async def test_healthz_is_public_and_reveals_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    # No version, no internals: the body carries liveness and nothing else.
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_every_response_carries_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    request_id = response.headers["x-request-id"]
    assert REQUEST_ID_PATTERN.match(request_id)


@pytest.mark.asyncio
async def test_request_ids_are_unique_per_request(client: httpx.AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.asyncio
async def test_security_headers_are_set_by_the_app(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_client_supplied_request_id_is_not_echoed(client: httpx.AsyncClient) -> None:
    """A caller must not be able to choose the correlation id used in our logs."""
    forged = "req_deadbeefdeadbeefdeadbeefdeadbeef"

    response = await client.get("/healthz", headers={"X-Request-Id": forged})

    assert response.headers["x-request-id"] != forged
