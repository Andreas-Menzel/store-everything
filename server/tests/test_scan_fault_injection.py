"""Kill the importer mid-tree, restart it, and check the tree came out once.

This is [F-001/AC-3](../../features/F-001-upload-and-import.md) — "killing the importer at
each injected fault point and restarting yields the same final state, with no duplicate
registrations" — and the reason F-001/FR-5 declares `*(verify: fault-injection)*`. A 10 TB
import runs for hours across deploys and power cuts, so "resumable" has to be a property of
the schema rather than a hope about timing.

The seam under attack is the checkpoint: one directory's registrations, its discoveries and
its removal from the frontier commit together. A crash **before** that commit must cost only
that directory; a crash **after** it must not repeat the directory. The crash is a real
`os._exit(137)` inside a real `Runner`, so there is no unwinding and no `finally` — exactly
what a power cut does — and the fault points are armed in the production code path, not in a
test's own copy of the loop.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

from store_everything.config import Settings
from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE
from store_everything.tables import file, folder, scan_frontier, scan_run
from tests.test_scanning import adopt, build

pytestmark = [pytest.mark.integration, pytest.mark.fault_injection, pytest.mark.asyncio]

SERVER_ROOT = Path(__file__).resolve().parents[1]

#: Both sides of the checkpoint, in execution order.
CHECKPOINT_FAULT_POINTS = ("scan.after-batch", "scan.after-commit")

#: Wide and deep enough that a crash lands mid-tree rather than before or after all of it.
TREE = {
    "a.txt": b"one",
    "Photos/b.txt": b"two",
    "Photos/2026/c.txt": b"three",
    "Photos/2026/summer/d.txt": b"four",
    "Documents/e.txt": b"five",
    "Documents/tax/f.txt": b"six",
}

_WORKER_SCRIPT = """
import asyncio, sys
from store_everything import scanning
from store_everything.config import Settings
from store_everything.db import create_engine
from store_everything.runner import Runner


async def main(url: str) -> None:
    settings = Settings(
        database_url=url, app_env="development", log_level="CRITICAL", worker_concurrency=1
    )
    engine = create_engine(settings)
    try:
        runner = Runner(engine, settings, {scanning.KIND: scanning.scan}, worker=sys.argv[2])
        await runner.run_once()
    finally:
        await engine.dispose()


asyncio.run(main(sys.argv[1]))
"""


def run_worker(database_url: str, *, worker: str, crash_at: str | None) -> int:
    """One claim-and-execute cycle in a fresh process, optionally killed mid-scan."""
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


def observe(database_url: str) -> dict[str, int]:
    """What the database holds: registrations, folders, runs, and pending directories."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return {
                table.name: int(
                    connection.execute(select(func.count()).select_from(table)).scalar_one()
                )
                for table in (file, folder, scan_run, scan_frontier)
            }
    finally:
        engine.dispose()


def expire_lease(database_url: str) -> None:
    """Back-date the lease of whatever the killed worker was holding.

    A crashed worker does not release anything — its lease simply lapses, and the claim
    query's expired-lease branch *is* the recovery path (12 § leases & fencing). Expiring it
    in SQL exercises that branch without a test that sleeps.
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


def paths_registered(database_url: str) -> int:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return int(connection.execute(select(func.count()).select_from(file)).scalar_one())
    finally:
        engine.dispose()


@pytest.mark.fr("F-001/FR-5")
@pytest.mark.parametrize("crash_at", CHECKPOINT_FAULT_POINTS)
async def test_an_importer_killed_mid_tree_converges_on_restart(
    identity_settings: Settings, identity_database: str, tmp_path: Path, crash_at: str
) -> None:
    """Crash, restart, crash again, restart — the tree still registers exactly once."""
    tree = tmp_path / "nas"
    build(tree, TREE)
    before = {path: hashlib.sha256(payload).hexdigest() for path, payload in TREE.items()}

    async with adopt(identity_settings, identity_database, tree) as (_client, _workspace):
        pass

    # Two interrupted attempts, so the resume path runs on top of a partial run rather than
    # only on a fresh one.
    for attempt in range(2):
        assert (
            run_worker(identity_database, worker=f"crash/{attempt}", crash_at=crash_at)
            == CRASH_EXIT_STATUS
        ), "the worker was expected to be killed at the fault point"
        # Whatever was registered before the crash is a prefix of the truth, never a duplicate.
        interrupted = observe(identity_database)
        assert interrupted["file"] <= len(TREE)
        # And the crash really landed mid-tree: directories are still queued. Without this the
        # convergence assertions below could pass on a scan that never started.
        assert interrupted["scan_frontier"] >= 1
        if crash_at == "scan.after-commit":
            # This variant crashes *after* a checkpoint, so partial progress is durable —
            # which is the state the restart has to resume on top of.
            assert interrupted["file"] >= 1
        expire_lease(identity_database)

    assert run_worker(identity_database, worker="finish", crash_at=None) == 0

    counted = observe(identity_database)
    assert counted["file"] == len(TREE), "a restart duplicated or lost a registration"
    # root + Photos + Photos/2026 + .../summer + Documents + Documents/tax
    assert counted["folder"] == 6
    assert counted["scan_frontier"] == 0, "the run left directories on the frontier"
    # One run throughout: a re-claimed operation resumes its own run rather than starting one.
    assert counted["scan_run"] == 1

    # And the tree itself is untouched by any of it.
    assert {
        str(path.relative_to(tree)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(tree.rglob("*"))
        if path.is_file() and not str(path.relative_to(tree)).startswith(".workspace")
    } == before
