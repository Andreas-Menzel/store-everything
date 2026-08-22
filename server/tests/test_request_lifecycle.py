"""When the request transaction commits — and what the client is told when it cannot.

One ordering decides whether a `2xx` means anything: a dependency with `yield` whose exit
code runs *after* the response has been sent cannot change that response, so a `COMMIT`
that failed there would leave the client holding a success for rows PostgreSQL threw away
(12-reliability.md § the request transaction; 08-api-principles.md § errors).

The first two tests pin the ordering itself against the installed FastAPI, because the
guarantee is a framework behaviour we depend on rather than code of ours that a reader can
check. The last one proves the consequence on the real API with a real database, and the
failure it injects is not simulated: a deferred constraint trigger makes the `COMMIT`
itself raise — the same shape as the containment check the cross-workspace folder move
defers to commit time.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.db import DatabaseConnection
from store_everything.problems import PROBLEM_MEDIA_TYPE
from store_everything.tables import access_token
from tests.identity_helpers import SAME_ORIGIN, login

pytestmark = pytest.mark.asyncio

BASE_URL = "http://testserver"


# --------------------------------------------------------------- the ordering, in isolation


class _RecordingConnection:
    """Stands in for the request's connection and records what the lifecycle does to it."""

    def __init__(self, observed: list[str]) -> None:
        self._observed = observed

    async def commit(self) -> None:
        self._observed.append("commit")

    async def rollback(self) -> None:
        self._observed.append("rollback")


class _RecordingEngine:
    def __init__(self, observed: list[str]) -> None:
        self._observed = observed

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[_RecordingConnection]:
        yield _RecordingConnection(self._observed)


def _probe_app(observed: list[str], *, fail: bool) -> ASGIApp:
    """A one-route app wired to the real dependency, wrapped in a send observer.

    The observer is where `http.response.start` becomes visible: everything before it is
    still changeable, everything after it is already on the wire.
    """
    app = FastAPI()
    app.state.engine = _RecordingEngine(observed)

    @app.post("/probe")
    async def probe(  # pyright: ignore[reportUnusedFunction]
        connection: DatabaseConnection,
    ) -> dict[str, bool]:
        observed.append("handler")
        if fail:
            raise HTTPException(status_code=409, detail="refused")
        return {"ok": True}

    async def observer(scope: Scope, receive: Receive, send: Send) -> None:
        async def record(message: Message) -> None:
            if message["type"] == "http.response.start":
                observed.append("response.start")
            await send(message)

        await app(scope, receive, record)

    return observer


async def _post_probe(observed: list[str], *, fail: bool = False) -> httpx.Response:
    transport = httpx.ASGITransport(app=_probe_app(observed, fail=fail))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        return await client.post("/probe")


async def test_the_request_transaction_commits_before_the_response_starts() -> None:
    """The guarantee `db.DatabaseConnection` claims, asserted rather than assumed.

    Red on any FastAPI release that moves the teardown back behind the response — which is
    what `scope="function"` buys and what a version pin alone would not notice.
    """
    observed: list[str] = []

    response = await _post_probe(observed)

    assert response.status_code == 200
    assert observed == ["handler", "commit", "response.start"]


async def test_a_refused_request_rolls_back_before_its_answer_is_sent() -> None:
    """The other half: nothing is committed, and the rollback still precedes the answer."""
    observed: list[str] = []

    response = await _post_probe(observed, fail=True)

    assert response.status_code == 409
    assert observed == ["handler", "rollback", "response.start"]


# ------------------------------------------------------- the consequence, on the real API


#: Makes `COMMIT` fail for real. A constraint trigger deferred to commit time is the same
#: mechanism as the deferred containment check on a cross-workspace move, so the failure
#: arrives exactly where a hand-rolled mock could not put it.
_REFUSE_AT_COMMIT = (
    """
    CREATE FUNCTION refuse_at_commit() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'refused at commit'; END
    $$
    """,
    """
    CREATE CONSTRAINT TRIGGER refuse_every_token AFTER INSERT ON access_token
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION refuse_at_commit()
    """,
)


async def _arm_deferred_failure(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for statement in _REFUSE_AT_COMMIT:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _count_tokens(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            query = select(func.count()).select_from(access_token)
            return (await connection.execute(query)).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_commit_that_fails_is_never_answered_as_success(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    """`201 Created` for a token that does not exist is the one answer the API may not give."""
    await login(identity_client)
    await _arm_deferred_failure(identity_database)

    response = await identity_client.post(
        f"{API_V1_PREFIX}/auth/tokens",
        json={"name": "agent", "scope": "read"},
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["title"] == "Internal server error"
    # And the row the `201` would have promised is not there.
    assert await _count_tokens(identity_database) == 0
