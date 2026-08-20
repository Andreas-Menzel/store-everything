"""Abuse protection (07-identity-permissions-sharing.md § abuse protection).

Two mechanisms, deliberately different because they defend against different things:

1. **Credential lockout** — durable, derived from the event log. Failed logins are already
   recorded there (they are security events), so counting them needs no second table and
   survives a restart: an attacker cannot reset the counter by making the process die,
   which is exactly the property an in-memory counter lacks.
2. **Request ceiling** — in-process, per credential or client address, for everything under
   `/api/v1`. This one is a backstop for accidental hammering; volumetric abuse belongs to
   the edge (ADR-0009), and losing these counters on restart costs nothing.

Both refusals answer `429` and are recorded once per window as `auth.rate_limited`, so an
operator sees abuse in the audit trail rather than only in logs.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events
from store_everything.events import Actor
from store_everything.problems import ProblemException
from store_everything.tables import event

#: Beyond this many distinct keys the window map is swept for idle entries. A limiter must
#: not become a memory leak an attacker can grow by rotating addresses.
_SWEEP_THRESHOLD = 4096


class TooManyRequests(ProblemException):
    """`429` with `Retry-After`, so a well-behaved client knows when to come back."""

    def __init__(self, *, detail: str, retry_after_seconds: int) -> None:
        super().__init__(
            status=429,
            slug="too-many-requests",
            title="Too many requests",
            detail=detail,
            headers={"Retry-After": str(retry_after_seconds)},
        )


class RequestLimiter:
    """A fixed-window-free sliding counter: timestamps per key, pruned on read."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._window = 60.0
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self._hits) > _SWEEP_THRESHOLD:
            self._sweep(now)

        hits = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= self._per_minute:
            return False

        hits.append(now)
        return True

    def _sweep(self, now: float) -> None:
        cutoff = now - self._window
        for key in [key for key, hits in self._hits.items() if not hits or hits[-1] < cutoff]:
            del self._hits[key]


async def note_refusal(
    connection: AsyncConnection,
    *,
    scope: str,
    key: str,
    window: timedelta,
    client_ip: str | None,
) -> None:
    """Record a refusal — at most once per key per window, so a flood cannot bloat the log.

    Committed by the caller. Refusals are recorded even though the request fails, which is
    the one place an event outlives its failed request on purpose.
    """
    cutoff = datetime.now(UTC) - window
    existing = await connection.execute(
        select(event.c.id)
        .where(
            event.c.action == events.RATE_LIMITED,
            event.c.occurred_at > cutoff,
            event.c.details["scope"].astext == scope,
            event.c.details["key"].astext == key,
        )
        .limit(1)
    )
    if existing.first() is not None:
        return

    await events.record(
        connection,
        action=events.RATE_LIMITED,
        resource_type=events.RESOURCE_SESSION if scope == "login" else events.RESOURCE_USER,
        actor=Actor.system(),
        details={"scope": scope, "key": key},
        client_ip=client_ip,
    )


async def login_attempts_exhausted(
    connection: AsyncConnection,
    *,
    email: str,
    client_ip: str | None,
    max_attempts: int,
    window: timedelta,
) -> bool:
    """True when recent failures for this identity — or from this address — hit the ceiling.

    Counted over a sliding window, so it heals by itself: after a quiet window the ceiling
    is lifted without an operator unlocking anything.
    """
    cutoff = datetime.now(UTC) - window

    by_email = func.count().filter(event.c.details["email"].astext == email)
    columns = [by_email.label("by_email")]
    if client_ip is not None:
        columns.append(func.count().filter(event.c.client_ip == client_ip).label("by_ip"))

    result = await connection.execute(
        select(*columns).where(
            and_(event.c.action == events.LOGIN_FAILED, event.c.occurred_at > cutoff)
        )
    )
    counts = tuple(result.one())
    return any(count >= max_attempts for count in counts)
