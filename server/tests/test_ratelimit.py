"""Abuse protection: the in-process ceiling and its audit trail.

The login lockout is exercised through the API in `test_identity_api.py`, since it is the
event log that counts. What is left here is the request ceiling — a counter, so its edges
(window expiry, key isolation, memory growth) are worth checking directly — and the
observable behaviour of hitting it.
"""

from __future__ import annotations

import httpx
import pytest

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.app import create_app
from store_everything.problems import problem_type
from store_everything.ratelimit import RequestLimiter
from tests.conftest import make_settings
from tests.identity_helpers import BASE_URL, login, read_events


def test_it_allows_up_to_the_ceiling_then_refuses() -> None:
    limiter = RequestLimiter(per_minute=3)

    assert [limiter.allow("caller") for _ in range(5)] == [True, True, True, False, False]


def test_callers_do_not_share_a_budget() -> None:
    """One noisy token must not spend another user's allowance."""
    limiter = RequestLimiter(per_minute=1)

    assert limiter.allow("first")
    assert limiter.allow("second")
    assert not limiter.allow("first")


def test_the_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_000.0]
    monkeypatch.setattr("store_everything.ratelimit.time.monotonic", lambda: clock[0])
    limiter = RequestLimiter(per_minute=2)

    assert limiter.allow("caller")
    assert limiter.allow("caller")
    assert not limiter.allow("caller")

    clock[0] += 61
    assert limiter.allow("caller")


def test_idle_keys_are_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise an attacker rotating addresses grows the map without bound."""
    clock = [1_000.0]
    monkeypatch.setattr("store_everything.ratelimit.time.monotonic", lambda: clock[0])
    limiter = RequestLimiter(per_minute=10)

    for index in range(5_000):
        limiter.allow(f"ip:{index}")
    clock[0] += 61
    limiter.allow("ip:fresh")

    assert len(limiter._hits) < 5_000  # pyright: ignore[reportPrivateUsage]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exceeding_the_ceiling_answers_429_and_is_recorded_once(
    identity_database: str,
) -> None:
    settings = make_settings(
        database_url=identity_database,
        bootstrap_admin_email="admin@example.com",
        bootstrap_admin_password="bootstrap-password-1",
        session_cookie_secure=False,
        rate_limit_per_minute=4,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await login(client)  # spends one

            statuses = [
                (await client.get(f"{API_V1_PREFIX}/auth/me")).status_code for _ in range(6)
            ]

            refused = await client.get(f"{API_V1_PREFIX}/auth/me")

    assert 429 in statuses
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "60"
    assert refused.json()["type"] == problem_type("too-many-requests")

    # Recorded for the operator, but once per window rather than once per refused request.
    refusals = await read_events(identity_database, action="auth.rate_limited")
    assert len(refusals) == 1
    assert refusals[0]["details"]["scope"] == "api"
    # The key is a digest of the credential, never the credential.
    assert refusals[0]["details"]["key"].startswith("credential:")
