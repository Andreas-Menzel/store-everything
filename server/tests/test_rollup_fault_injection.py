"""Kill the rollup mid-batch, restart it, and check the numbers came out once.

[F-015/FR-8](../../features/F-015-folders.md)'s whole simplification is that the statement which
claims a batch of deltas is the statement that adds them up: the queue rows disappear and the
totals move in one transaction, so there is no state in which a change has been counted but not
consumed — or consumed but not counted.

That claim is about a *crash*, so it is tested with one. `rollup.after-batch` sits between the
batch's statement and its commit, which is the only instant where the two could come apart, and
the process really dies there — `os._exit(137)`, no unwinding, no `finally`.

Afterwards the deltas must still be queued, the totals must still be untouched, and a clean run
must land them exactly once. Anything else is either a lost upload or a double count, and both are
worse than being slow.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

from store_everything.config import Settings
from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE
from store_everything.tables import folder_aggregate, folder_delta
from tests.test_scanning import adopt, build
from tests.workspace_helpers import rollup_pending, scan_pending

pytestmark = [pytest.mark.integration, pytest.mark.fault_injection, pytest.mark.asyncio]

SERVER_ROOT = Path(__file__).resolve().parents[1]

TREE = {
    "a.txt": b"one",
    "Photos/b.txt": b"two",
    "Photos/2026/c.txt": b"three",
    "Documents/d.txt": b"four",
}

_WORKER_SCRIPT = """
import asyncio, sys
from store_everything import handlers
from store_everything.config import Settings
from store_everything.db import create_engine
from store_everything.runner import Runner


async def main(url: str) -> None:
    settings = Settings(
        database_url=url,
        app_env="development",
        log_level="CRITICAL",
        worker_concurrency=1,
        app_data_root=sys.argv[3],
    )
    engine = create_engine(settings)
    try:
        # Only the rollup kind, or this worker could claim the janitor and exit cleanly without
        # ever reaching the point under test.
        rollup = {"workspace.rollup": handlers.registry(settings)["workspace.rollup"]}
        runner = Runner(engine, settings, rollup, worker=sys.argv[2])
        await runner.run_once()
    finally:
        await engine.dispose()


asyncio.run(main(sys.argv[1]))
"""


def run_worker(database_url: str, *, worker: str, crash_at: str | None, app_data_root: Path) -> int:
    """One claim-and-execute cycle in a fresh process, optionally killed mid-drain."""
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "development"
    environment["SE_DATABASE_URL"] = database_url
    if crash_at is not None:
        environment[FAULT_POINT_VARIABLE] = crash_at
    else:
        environment.pop(FAULT_POINT_VARIABLE, None)

    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _WORKER_SCRIPT, database_url, worker, str(app_data_root)],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    return completed.returncode


def expire_lease(database_url: str) -> None:
    """Back-date the killed worker's lease, the way time would.

    A crashed worker releases nothing — its lease lapses, and the claim query's expired-lease
    branch *is* the recovery path (12 § leases & fencing). Expiring it in SQL exercises that
    branch without a test that sleeps.
    """
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "UPDATE operation SET lease_expires_at = now() - interval '1 minute' "
                    "WHERE state = 'running'"
                )
            )
            connection.commit()
    finally:
        engine.dispose()


def observe(database_url: str) -> tuple[int, int]:
    """How much is still queued, and how much has been counted."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            queued = connection.execute(select(func.count()).select_from(folder_delta)).scalar_one()
            counted = connection.execute(
                select(func.coalesce(func.sum(folder_aggregate.c.total_files), 0))
            ).scalar_one()
            return queued, counted
    finally:
        engine.dispose()


@pytest.mark.fr("F-015/FR-8")
async def test_a_rollup_killed_before_its_commit_loses_nothing_and_counts_nothing_twice(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    contents = build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (_client, _workspace):
        # The import queues one delta per registered file and asks for a rollup.
        await scan_pending(identity_database, identity_settings)

        queued, counted = observe(identity_database)
        assert queued == len(contents), "the import queued a delta per file"
        assert counted == 0, "and nothing has drained them yet"

        killed = run_worker(
            identity_database,
            worker="test/crashing",
            crash_at="rollup.after-batch",
            app_data_root=tmp_path / "app",
        )
        assert killed == CRASH_EXIT_STATUS, "the fault point has to be reachable to be a test"

        interrupted, still_counted = observe(identity_database)
        assert interrupted == queued, "the batch was claimed and the claim died with it"
        assert still_counted == 0, "so no folder's total moved either"

        # A clean run — through the same registry, claiming what the dead worker left behind.
        expire_lease(identity_database)
        await rollup_pending(identity_database, identity_settings)

        drained, finally_counted = observe(identity_database)
        assert drained == 0, "the retry consumed what the crash left behind"
        # Each file counted exactly once by each folder above it, so the sum over folders is the
        # sum of the files' depths: 1 in the root, 2 under `Photos`, 3 under `Photos/2026`, 2 under
        # `Documents`. A delta applied twice, or dropped, shows up here as any other number.
        assert finally_counted == 8
