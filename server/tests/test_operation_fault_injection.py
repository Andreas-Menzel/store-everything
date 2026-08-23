"""Kill the worker at the seam between effect and record, and check convergence.

12-reliability.md's binding property, applied to the operation layer:

> After any prefix of any operation, plus a restart, the system converges to the same
> terminal state — no debris past its grace window, no duplicated effects, no duplicated
> events.

The seam worth attacking is the one between a handler's writes and the transition that
records them. They share a transaction, so the claim is that *no* crash can leave the effect
applied without the operation succeeding, or the operation succeeded without the effect. A
real `kill -9` is the only way to test it: `os._exit` in a subprocess, no unwinding, no
`finally`. It is `kill -9` rather than a power cut — the page cache survives it — so what is
proven here is *ordering*, not the presence of the durability barriers.

The crash points are armed in the production code path (`runner.py`), not in a test double —
a fault a test injects into its own copy of the logic proves nothing about the real one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE
from store_everything.tables import app_user, operation

pytestmark = pytest.mark.integration

SERVER_ROOT = Path(__file__).resolve().parents[1]

#: Every point at which the worker can be interrupted around the transition, in order.
SEAM_FAULT_POINTS = (
    "operation.after-handler",
    "operation.after-success-transition",
    "operation.after-commit",
)

#: The effect the handler applies. Its uniqueness constraint is what would catch a double
#: application: a second `create_user` with this address raises rather than passing quietly.
EFFECT_EMAIL = "effect@example.com"

_WORKER_SCRIPT = """
import asyncio, sys
from store_everything import identity, operations
from store_everything.config import Settings
from store_everything.events import Actor
from store_everything.runner import Job, Runner
from store_everything.db import create_engine

KIND = "test.effect"


async def handler(job: Job) -> None:
    await identity.create_user(
        job.connection,
        email="effect@example.com",
        display_name="Effect",
        password="a-long-enough-password",
        role="member",
        actor=Actor.system(),
    )


async def main(url: str) -> None:
    settings = Settings(
        database_url=url, app_env="development", log_level="CRITICAL", worker_concurrency=1
    )
    engine = create_engine(settings)
    try:
        runner = Runner(engine, settings, {KIND: handler}, worker=sys.argv[2])
        await runner.run_once()
    finally:
        await engine.dispose()


asyncio.run(main(sys.argv[1]))
"""


def run_worker(database_url: str, *, worker: str, crash_at: str | None) -> int:
    """Run one claim-and-execute cycle in a fresh process, optionally killing it."""
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "development"
    environment["SE_DATABASE_URL"] = database_url
    if crash_at is not None:
        environment[FAULT_POINT_VARIABLE] = crash_at
    else:
        environment.pop(FAULT_POINT_VARIABLE, None)

    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _WORKER_SCRIPT, database_url, worker],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    return completed.returncode


def enqueue_effect(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "INSERT INTO operation (id, kind, state, max_attempts, priority) "
                    "VALUES (gen_random_uuid(), 'test.effect', 'queued', 5, 2)"
                )
            )
            connection.commit()
    finally:
        engine.dispose()


def observe(database_url: str) -> tuple[str, int]:
    """The operation's state and how many times the effect was applied."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            state = (
                connection.execute(
                    select(operation.c.state).where(operation.c.kind == "test.effect")
                )
            ).scalar_one()
            effects = (
                connection.execute(
                    select(func.count())
                    .select_from(app_user)
                    .where(app_user.c.email == EFFECT_EMAIL)
                )
            ).scalar_one()
            return str(state), int(effects)
    finally:
        engine.dispose()


def expire_lease(database_url: str) -> None:
    """Age out the dead worker's lease, as time would."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            # Only running rows hold a lease; ageing a finished one would violate the
            # constraint that says a lease always has an owner.
            connection.execute(
                text(
                    "UPDATE operation SET lease_expires_at = now() - interval '1 hour' "
                    "WHERE state = 'running'"
                )
            )
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.fault_injection
@pytest.mark.parametrize("crash_at", SEAM_FAULT_POINTS)
def test_a_worker_killed_at_the_seam_converges_exactly_once(
    identity_database: str, crash_at: str
) -> None:
    enqueue_effect(identity_database)

    assert run_worker(identity_database, worker="victim/1", crash_at=crash_at) == CRASH_EXIT_STATUS

    # Nothing is half-applied: either the transaction committed or it did not.
    state, effects = observe(identity_database)
    assert effects in (0, 1)
    assert (state == "succeeded") == (effects == 1), (
        f"effect and record disagree after a crash at {crash_at}: {state=} {effects=}"
    )

    # Recovery is the normal path: the successor reclaims the expired lease and finishes.
    expire_lease(identity_database)
    assert run_worker(identity_database, worker="successor/2", crash_at=None) == 0

    state, effects = observe(identity_database)
    assert state == "succeeded"
    assert effects == 1, "the effect was applied twice — the retry duplicated work"


@pytest.mark.fault_injection
def test_an_uninterrupted_run_is_the_same_terminal_state(identity_database: str) -> None:
    """The control case: the convergence assertions above must not be vacuous."""
    enqueue_effect(identity_database)

    assert run_worker(identity_database, worker="clean/1", crash_at=None) == 0

    assert observe(identity_database) == ("succeeded", 1)
