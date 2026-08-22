"""Kill the importer mid-tree, restart it, and check the tree came out once.

This is [F-001/AC-3](../../features/F-001-upload-and-import.md) — "killing the importer at
each injected fault point and restarting yields the same final state, with no duplicate
registrations" — and the reason F-001/FR-5 declares `*(verify: fault-injection)*`. A 10 TB
import runs for hours across deploys and power cuts, so "resumable" has to be a property of
the schema rather than a hope about timing.

Two seams are under attack, and both are checkpoints. The **traversal's**: one directory's
registrations, its discoveries and its removal from the frontier commit together, so a crash
before that commit costs only that directory and a crash after it must not repeat the
directory. The **sweep's**: one batch of vanished files becomes trash entries in one
transaction, and a crash on either side must leave exactly one entry per file — no file half
deleted, no deletion concluded twice.

The crash is a real `os._exit(137)` inside a real `Runner`, so there is no unwinding and no
`finally` — exactly what a power cut does — and the fault points are armed in the production
code path, not in a test's own copy of the loop.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import verify
from store_everything.config import Settings
from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE
from store_everything.tables import (
    file,
    folder,
    folder_closure,
    scan_frontier,
    scan_run,
    trash_entry,
)
from tests.test_scanning import adopt, build

pytestmark = [pytest.mark.integration, pytest.mark.fault_injection, pytest.mark.asyncio]

SERVER_ROOT = Path(__file__).resolve().parents[1]


async def audits_clean(database_url: str, settings: Settings) -> str:
    """The `verify` audit's own words, so a failure names the finding rather than a `False`.

    Spec 11 asks for this after *every* fault-injection test, and the phase-1 exit criterion
    names it directly ([12 § verification](../../specs/12-reliability.md#verification)): a
    recovery that converges the rows but leaves staging debris or an unreferenced blob behind
    has not converged, it has only stopped being visible.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await verify.audit(connection, settings=settings)).render()
    finally:
        await engine.dispose()


#: Both sides of the checkpoint, in execution order.
CHECKPOINT_FAULT_POINTS = ("scan.after-batch", "scan.after-commit")

#: Both sides of the *sweep's* checkpoint — the transaction that turns "this file is gone" into
#: trash entries (F-001/FR-6).
SWEEP_FAULT_POINTS = ("scan.after-sweep-batch", "scan.after-sweep-commit")

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
        # The registry's own closure, so this crashes the real thing — but only the scan kind
        # is registered, or this worker could claim the janitor instead and exit cleanly.
        scan = {"workspace.scan": handlers.registry(settings)["workspace.scan"]}
        runner = Runner(engine, settings, scan, worker=sys.argv[2])
        await runner.run_once()
    finally:
        await engine.dispose()


asyncio.run(main(sys.argv[1]))
"""


def run_worker(database_url: str, *, worker: str, crash_at: str | None, app_data_root: Path) -> int:
    """One claim-and-execute cycle in a fresh process, optionally killed mid-scan."""
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
            run_worker(
                identity_database,
                worker=f"crash/{attempt}",
                crash_at=crash_at,
                app_data_root=identity_settings.app_data_root,
            )
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

    assert (
        run_worker(
            identity_database,
            worker="finish",
            crash_at=None,
            app_data_root=identity_settings.app_data_root,
        )
        == 0
    )

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

    # Two crashes and a restart later, the instance has nothing to report about itself.
    assert "clean" in await audits_clean(identity_database, identity_settings)


def make_due(database_url: str) -> None:
    """Bring the workspace's next scheduled scan forward, so a worker claims it now.

    The equivalent of `operations.expedite`, in SQL because this module drives the database
    directly and has no app to ask.
    """
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "UPDATE operation SET next_due_at = now() "
                    "WHERE kind = 'workspace.scan' AND state = 'queued'"
                )
            )
            connection.commit()
    finally:
        engine.dispose()


def trashed_paths(database_url: str) -> list[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return sorted(connection.execute(select(trash_entry.c.path)).scalars().all())
    finally:
        engine.dispose()


@pytest.mark.fr("F-001/FR-5", "F-001/FR-6")
@pytest.mark.parametrize("crash_at", SWEEP_FAULT_POINTS)
async def test_a_reconciling_scan_killed_mid_sweep_converges(
    identity_settings: Settings, identity_database: str, tmp_path: Path, crash_at: str
) -> None:
    """Kill the worker while it is turning vanished files into trash entries.

    The sweep is the other half of the same promise: a crash may not leave a file half-deleted —
    trashed with no entry, or an entry with no state — and a restart may not conclude the
    deletion twice. It carries no cursor of its own, so this is really a test that the query
    ("live files this run did not see") *is* the cursor.
    """
    tree = tmp_path / "nas"
    build(tree, TREE)
    gone = ["Photos/b.txt", "Photos/2026/c.txt", "Documents/e.txt"]

    async with adopt(identity_settings, identity_database, tree) as (_client, _workspace):
        pass
    assert (
        run_worker(
            identity_database,
            worker="import",
            crash_at=None,
            app_data_root=identity_settings.app_data_root,
        )
        == 0
    )
    assert observe(identity_database)["file"] == len(TREE)

    for relative in gone:
        (tree / relative).unlink()

    make_due(identity_database)
    assert (
        run_worker(
            identity_database,
            worker="crash",
            crash_at=crash_at,
            app_data_root=identity_settings.app_data_root,
        )
        == CRASH_EXIT_STATUS
    ), "the worker was expected to be killed in the sweep"
    # A crash *before* the commit costs the batch; after it, the work is already durable.
    interrupted = trashed_paths(identity_database)
    assert interrupted == ([] if crash_at == "scan.after-sweep-batch" else sorted(gone))
    expire_lease(identity_database)

    assert (
        run_worker(
            identity_database,
            worker="finish",
            crash_at=None,
            app_data_root=identity_settings.app_data_root,
        )
        == 0
    )

    # Exactly the files that vanished, exactly once each, whichever side of the commit died.
    assert trashed_paths(identity_database) == sorted(gone)
    assert observe(identity_database)["file"] == len(TREE), "no registration was lost"
    live = live_paths(identity_database)
    assert sorted(live) == sorted(set(TREE) - set(gone))


def live_paths(database_url: str) -> list[str]:
    """The paths still live, derived the way the app derives them."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                select(file.c.id, file.c.name, file.c.folder_id).where(file.c.state == "live")
            ).all()
            names: list[str] = []
            for _, name, folder_id in rows:
                segments = (
                    connection.execute(
                        select(folder.c.name)
                        .join(
                            folder_closure,
                            folder_closure.c.ancestor_id == folder.c.id,
                        )
                        .where(folder_closure.c.descendant_id == folder_id)
                        .order_by(folder_closure.c.depth.desc())
                    )
                    .scalars()
                    .all()
                )
                names.append("/".join([*(part for part in segments if part), name]))
            return names
    finally:
        engine.dispose()


@pytest.mark.fr("F-015/FR-7")
async def test_a_folder_identity_transfer_survives_a_crash_before_its_commit(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The identity pass is one transaction, and this is what makes that safe.

    Its evidence — which directory's content turned up where — belongs to the *run*, and a crashed
    scan operation resumes its own run rather than starting a second one. So a crash before the
    pass commits costs nothing: the resumed run reads the same evidence and transfers the same
    folder. Losing that evidence would mean a renamed directory silently getting a new identity,
    taking every grant and tag on it out of reach.
    """
    tree = tmp_path / "nas"
    build(tree, {"Album/a.txt": b"one", "Album/b.txt": b"two"})

    async with adopt(identity_settings, identity_database, tree) as (_client, _workspace):
        pass
    assert (
        run_worker(
            identity_database,
            worker="import",
            crash_at=None,
            app_data_root=identity_settings.app_data_root,
        )
        == 0
    )
    engine = create_engine(identity_database)
    try:
        with engine.connect() as connection:
            album = connection.execute(
                select(folder.c.id).where(folder.c.name == "Album")
            ).scalar_one()
    finally:
        engine.dispose()

    (tree / "Album").rename(tree / "Photos")
    request_rescan(identity_database)

    assert (
        run_worker(
            identity_database,
            worker="crash/identities",
            crash_at="scan.after-identities",
            app_data_root=identity_settings.app_data_root,
        )
        == CRASH_EXIT_STATUS
    ), "the fault point has to be reachable to be a test"
    assert named(identity_database, album) == "Album", "the transfer was not committed"

    expire_lease(identity_database)
    assert (
        run_worker(
            identity_database,
            worker="resume",
            crash_at=None,
            app_data_root=identity_settings.app_data_root,
        )
        == 0
    )

    assert named(identity_database, album) == "Photos", (
        "the resumed run read its own evidence and transferred the folder"
    )
    assert observe(identity_database)["scan_run"] == 2, "one run per scan, resumed not restarted"


def request_rescan(database_url: str) -> None:
    """Make the pending scan due now, the way the rescan endpoint does."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "UPDATE operation SET next_due_at = now() "
                    "WHERE kind = 'workspace.scan' AND state = 'queued'"
                )
            )
            connection.commit()
    finally:
        engine.dispose()


def named(database_url: str, folder_id: UUID) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return connection.execute(
                select(folder.c.name).where(folder.c.id == folder_id)
            ).scalar_one()
    finally:
        engine.dispose()
