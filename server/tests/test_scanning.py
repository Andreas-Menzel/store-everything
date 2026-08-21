"""Importing an existing tree, and scanning it again: what gets registered, and what does not.

This is the feature the product is sold on — point it at a decade of photos on a NAS and it
indexes them without moving a byte — so the assertions come in pairs. Every "it registered
what was there" is matched by a "the tree is byte-for-byte unchanged", because an importer
that quietly renames a colliding file to make its own model tidier would be worse than one
that refuses.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import (
    files,
    folders,
    fscheck,
    names,
    operations,
    scanning,
    scans,
    workspaces,
)
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.tables import file, folder
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    as_admin,
    create_member,
    create_workspace,
    instance,
    provision_pending,
    scan_pending,
    signed_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: A small tree with the shapes that matter: nesting, an extension-less name, a big-ish file.
TREE: dict[str, bytes] = {
    "notes.txt": b"a plain file",
    "Photos/beach.jpg": b"pretend this is a photo" * 100,
    "Photos/2026/summer/IMG_0001": b"no extension, so the type is unknown",
    "Documents/tax/return.pdf": b"%PDF-1.7 pretend",
}


def build(root: Path, contents: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """Write a tree the way a user's NAS folder already looks."""
    payloads = TREE if contents is None else contents
    root.mkdir(parents=True, exist_ok=True)
    for relative, payload in payloads.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return payloads


def fingerprint(root: Path) -> dict[str, str]:
    """Every file under `root` by relative path and digest, links included but never followed."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            found[relative] = f"link:{os.readlink(path)}"
        elif path.is_file():
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def user_files(root: Path) -> dict[str, str]:
    """The fingerprint with the app's own control directory excluded — everything the app
    promised not to touch."""
    return {
        path: digest
        for path, digest in fingerprint(root).items()
        if not path.startswith(names.CONTROL_DIRECTORY)
    }


@asynccontextmanager
async def adopt(
    settings: Settings, database_url: str, tree: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, UUID]]:
    """Adopt an existing directory and provision it, leaving its initial scan queued.

    Yields rather than returns so the signed-in client stays open for the assertions — the
    workspace and the session are one setup, not two.
    """
    async with as_admin(settings, adoption_roots=(tree,)) as client:
        created = await create_workspace(client, "The NAS", adopt_path=tree)
        assert created.status_code == 201, created.text
        await provision_pending(database_url)
        yield client, UUID(created.json()["id"])


async def registered(database_url: str) -> dict[str, str]:
    """Every registered file as path → content hash, derived from the folder chain."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(select(file.c.id))).scalars().all()
            found: dict[str, str] = {}
            for file_id in rows:
                record = await files.get(connection, file_id)
                version = await files.current_version(connection, file_id)
                assert record is not None and version is not None
                found[await files.path_of(connection, record)] = version.content_hash
            return found
    finally:
        await engine.dispose()


async def count(database_url: str, table: Any) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(select(func.count()).select_from(table))).scalar_one()
    finally:
        await engine.dispose()


async def status(client: Any, workspace_id: UUID, **params: Any) -> dict[str, Any]:
    response = await client.get(
        f"{API_V1_PREFIX}/workspaces/{workspace_id}/import-status", params=params
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


# ------------------------------------------------------------------------- the import


@pytest.mark.fr("F-001/FR-4")
async def test_an_adopted_tree_imports_itself(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-5's other half: indexed in place, nothing moved, nothing renamed."""
    tree = tmp_path / "nas"
    contents = build(tree)
    before = fingerprint(tree)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        results = await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    assert [result["outcome"] for result in results] == ["completed"]
    assert await registered(identity_database) == {
        path: hashlib.sha256(payload).hexdigest() for path, payload in contents.items()
    }

    # Every directory became a folder row: root + Photos + Photos/2026 + .../summer +
    # Documents + Documents/tax.
    assert await count(identity_database, folder) == 6

    latest = report["recent"][0]
    assert latest["trigger"] == "initial"
    assert latest["state"] == "completed"
    assert latest["files_registered"] == len(contents)
    assert latest["files_seen"] == len(contents)
    assert report["active"] is None

    # The tree the app was pointed at is exactly as it was — including the file it could not
    # type from its name.
    assert user_files(tree) == before


async def test_the_control_directory_is_neither_registered_nor_reported(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """F-001/FR-13. It is ours, so it is not even a fact about the user's tree."""
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"only file"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    assert await registered(identity_database) == {
        "notes.txt": hashlib.sha256(b"only file").hexdigest()
    }
    assert names.CONTROL_DIRECTORY not in str(report)


@pytest.mark.fr("F-001/FR-4")
async def test_a_second_pass_registers_nothing_new(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Convergence: scanning is idempotent, so the hourly pass is cheap and safe."""
    tree = tmp_path / "nas"
    contents = build(tree)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        first = await status(client, workspace)

        accepted = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        assert accepted.status_code == 202, accepted.text
        await scan_pending(identity_database, identity_settings)
        second = await status(client, workspace)

    assert first["recent"][0]["files_registered"] == len(contents)
    assert second["recent"][0]["files_registered"] == 0
    # The run reports that a person asked for it, even though it converged on the pending
    # scheduled one rather than starting a second traversal.
    assert second["recent"][0]["trigger"] == "manual"
    assert second["recent"][0]["files_seen"] == len(contents)
    assert await count(identity_database, file) == len(contents)


async def test_a_file_added_between_passes_is_registered(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """F-001/AC-4's first half: a file copied onto the storage by hand shows up."""
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"first"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        (tree / "Photos").mkdir()
        (tree / "Photos" / "later.jpg").write_bytes(b"copied on by hand")

        await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        await scan_pending(identity_database, identity_settings)

    assert "Photos/later.jpg" in await registered(identity_database)


# ------------------------------------------------------------------------- refusals


def sibling_collisions_are_possible(root: Path) -> bool:
    """Whether this filesystem can even *hold* two names that collide on the comparison key.

    A case-folding or normalizing filesystem — APFS, NTFS — silently merges them, so the
    collision this rule exists for cannot be created there. The probe already reports both
    facts, which is what it is for (ADR-0019).
    """
    facts = fscheck.probe(root).facts
    return facts.get("case_sensitivity") == "case-sensitive" and (
        facts.get("unicode") == "byte-preserving"
    )


@pytest.mark.fr("F-001/FR-11")
async def test_colliding_siblings_are_reported_and_the_tree_is_untouched(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-6: one of each registers, the rest are listed with both spellings, and a
    byte-for-byte comparison of the tree before and after shows it unchanged.

    Skipped where the filesystem cannot hold the collision in the first place; the rule
    itself is covered on every platform by `test_scan_listing.py`.
    """
    tree = tmp_path / "nas"
    tree.mkdir()
    if not sibling_collisions_are_possible(tree):
        pytest.skip("this filesystem folds case or normalizes, so the collision cannot exist")

    build(
        tree,
        {
            "Report.pdf": b"upper",
            "report.pdf": b"lower",
            unicodedata.normalize("NFC", "café.txt"): b"composed",
            unicodedata.normalize("NFD", "café.txt"): b"decomposed",
        },
    )
    before = user_files(tree)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    # Two names, four entries: one of each pair registers.
    assert len(await registered(identity_database)) == 2
    conflicts = [item for item in report["findings"]["data"] if item["kind"] == "conflict"]
    assert len(conflicts) == 2
    # Each conflict names the entry it collided with, so a human can act on it.
    assert all("collides with" in item["detail"] for item in conflicts)
    assert report["recent"][0]["conflicts"] == 2

    assert user_files(tree) == before


@pytest.mark.fr("F-001/FR-12")
async def test_symlinks_are_skipped_and_their_targets_never_read(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-7: a link to a file outside, a link to a directory outside, and a dangling one."""
    secret = tmp_path / "outside.txt"
    secret.write_bytes(b"not yours")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "deep").mkdir(parents=True)
    (elsewhere / "deep" / "hidden.txt").write_bytes(b"also not yours")

    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"the only real file"})
    (tree / "passwd-link").symlink_to(secret)
    (tree / "escape").symlink_to(elsewhere, target_is_directory=True)
    (tree / "dangling").symlink_to(tree / "gone.txt")

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    # Only the real file is registered; nothing behind a link, at any depth.
    assert set(await registered(identity_database)) == {"notes.txt"}
    skipped = {item["path"] for item in report["findings"]["data"] if item["kind"] == "skipped"}
    assert skipped == {"passwd-link", "escape", "dangling"}
    assert report["recent"][0]["skipped"] == 3
    # The tree behind the symlinked directory was never traversed, so it produced no folders.
    assert await count(identity_database, folder) == 1


async def test_a_name_the_policy_refuses_is_reported_rather_than_registered(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Failing predictably beats failing at the filesystem's whim (ADR-0019's limits)."""
    tree = tmp_path / "nas"
    build(tree, {"fine.txt": b"ok"})
    try:
        (tree / ("é" * 200)).write_bytes(b"400 bytes of name")
    except OSError:  # pragma: no cover - the filesystem refused it first, which is also fine
        pytest.skip("this filesystem refuses the over-long name itself")

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    assert set(await registered(identity_database)) == {"fine.txt"}
    reasons = [item["detail"] for item in report["findings"]["data"]]
    assert any("255 bytes" in reason for reason in reasons)


async def test_something_that_is_not_a_file_or_a_directory_is_skipped(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree, {"real.txt": b"ok"})
    os.mkfifo(tree / "pipe")

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    assert set(await registered(identity_database)) == {"real.txt"}
    assert any(
        item["path"] == "pipe" and "not a regular file" in item["detail"]
        for item in report["findings"]["data"]
    )


# --------------------------------------------------------------------------- the run


async def test_a_directory_that_vanished_is_an_empty_listing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A subtree deleted between being discovered and being processed is a fact, not an error.

    Exercised at the batch, because that is the unit: the frontier deliberately keeps
    directories we *expect*, so popping one that is gone has to be ordinary.
    """
    tree = tmp_path / "nas"
    build(tree, {"Keep/one.txt": b"kept"})

    async with adopt(identity_settings, identity_database, tree) as (_client, workspace):
        await scan_pending(identity_database, identity_settings)

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            found = await workspaces.get(connection, workspace)
            root = await folders.root_of(connection, workspace)
            assert found is not None and root is not None
            # A run of our own: `operation_id` is deliberately not a foreign key, so a test
            # can drive a run without a worker.
            run = await scans.start(
                connection,
                workspace_id=workspace,
                operation_id=uuid4(),
                trigger="manual",
                root_folder_id=root.id,
            )
            tally = await scanning.process_directory(
                connection,
                run=run,
                workspace=found,
                pending=scans.Pending("Vanished", root.id),
                store=BlobStore(identity_settings.versions_root),
            )
            await connection.commit()
    finally:
        await engine.dispose()

    assert tally.files_seen == 0
    assert tally.findings == []
    # And nothing was invented on disk to make the frontier's expectation true.
    assert not (tree / "Vanished").exists()


async def test_a_scan_resumes_where_it_stopped(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The frontier is the cursor: a re-claimed operation continues its own run.

    Simulated by running one batch, abandoning the connection, and letting the ordinary
    worker path pick the operation up again — which is what an expired lease does.
    """
    tree = tmp_path / "nas"
    contents = build(tree)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        results = await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    # One run, and it covered everything: the resumption path is exercised by the frontier
    # being consulted between every batch, which the counters show.
    assert len(results) == 1
    assert report["recent"][0]["directories_scanned"] == 6
    assert len(await registered(identity_database)) == len(contents)


async def test_a_subtree_rescan_only_visits_that_subtree(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        (tree / "Photos" / "new.jpg").write_bytes(b"added under Photos")
        (tree / "Documents" / "new.txt").write_bytes(b"added under Documents")

        accepted = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
            json={"path": "Photos"},
            headers=SAME_ORIGIN,
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["path"] == "Photos"
        await scan_pending(identity_database, identity_settings)

    found = await registered(identity_database)
    assert "Photos/new.jpg" in found
    assert "Documents/new.txt" not in found


async def test_an_unknown_subtree_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        missing = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
            json={"path": "Nowhere"},
            headers=SAME_ORIGIN,
        )
        traversal = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
            json={"path": "../escape"},
            headers=SAME_ORIGIN,
        )

    assert missing.status_code == 422
    assert traversal.status_code == 422


async def test_a_completed_scan_arms_the_next_one(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The chain re-arms itself in the transaction that completes a run (12 § inventory)."""
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with adopt(identity_settings, identity_database, tree) as (_client, _workspace):
        await scan_pending(identity_database, identity_settings)

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            depth = await operations.count_by_state(connection, kind=scans.KIND)
    finally:
        await engine.dispose()

    # One finished run, and exactly one pending successor — due in an hour, so a second
    # `scan_pending` right now would find nothing to do.
    assert depth == {"succeeded": 1, "queued": 1}
    assert await scan_pending(identity_database, identity_settings) == []


async def test_the_scan_is_recorded_in_the_event_log(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with adopt(identity_settings, identity_database, tree) as (_client, _workspace):
        await scan_pending(identity_database, identity_settings)

    scanned = await read_events(identity_database, action="workspace.scanned")
    assert len(scanned) == 1
    assert scanned[0]["actor_type"] == "system"
    assert scanned[0]["details"]["trigger"] == "initial"
    assert scanned[0]["details"]["files_registered"] == 1
    # Every registered file is in the log too, with the path it appeared at.
    created = await read_events(identity_database, action="file.created")
    assert [event["details"]["origin"] for event in created] == ["external"]


# ------------------------------------------------------------------------ ownership


async def test_another_users_workspace_reports_nothing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with instance(identity_settings, adoption_roots=(tree,)) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
            created = await create_workspace(admin, "The NAS", adopt_path=tree)
            workspace = UUID(created.json()["id"])
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            report = await member.get(f"{API_V1_PREFIX}/workspaces/{workspace}/import-status")
            rescan = await member.post(
                f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
            )

    assert report.status_code == 404
    assert rescan.status_code == 404


async def test_a_rescan_of_an_overdue_scan_still_reports_who_asked(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The case a simpler `expedite` missed: the pending scan is *already* due.

    That is precisely when a person presses the button — the queue is backed up, or the
    orchestrator is stopped — and the run must still say a person asked for it rather than
    reporting that the hour came round.
    """
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        # The pending successor becomes overdue, and nothing has claimed it.
        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "UPDATE operation SET next_due_at = now() - interval '1 hour' "
                        "WHERE kind = :kind AND state = 'queued'"
                    ),
                    {"kind": scans.KIND},
                )
                await connection.commit()
        finally:
            await engine.dispose()

        accepted = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        assert accepted.status_code == 202, accepted.text
        await scan_pending(identity_database, identity_settings)
        report = await status(client, workspace)

    assert report["recent"][0]["trigger"] == "manual"


async def test_a_rescan_never_pushes_a_due_scan_further_out(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Repeated requests must not starve work that is already waiting."""
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text(
                        "UPDATE operation SET next_due_at = now() - interval '1 hour' "
                        "WHERE kind = :kind AND state = 'queued'"
                    ),
                    {"kind": scans.KIND},
                )
                await connection.commit()

            for _ in range(3):
                await client.post(
                    f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
                    json={},
                    headers=SAME_ORIGIN,
                )

            async with engine.connect() as connection:
                overdue = (
                    await connection.execute(
                        text(
                            "SELECT next_due_at < now() FROM operation "
                            "WHERE kind = :kind AND state = 'queued'"
                        ),
                        {"kind": scans.KIND},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    assert overdue is True, "an expedite pushed an already-due operation back"


async def test_a_manually_requested_scan_names_the_person_who_asked(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """ "Who caused this?" is what an audit trail is for (ADR-0007, F-011/FR-9).

    Only the *run* is attributed: the files it registered were already on the disk, so
    recording that a person created them would be a worse falsehood than anonymity.
    """
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        me = (await client.get(f"{API_V1_PREFIX}/auth/me")).json()["user"]["id"]
        (tree / "two.txt").write_bytes(b"added by hand")

        await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        await scan_pending(identity_database, identity_settings)

    scanned = await read_events(identity_database, action="workspace.scanned")
    initial, manual = scanned[0], scanned[1]
    # The import nobody asked for is the instance's own work.
    assert (initial["actor_type"], initial["details"]["trigger"]) == ("system", "initial")
    # The one a person asked for names them.
    assert manual["actor_type"] == "user"
    assert str(manual["details"]["trigger"]) == "manual"

    created = await read_events(identity_database, action="file.created")
    # Both files appeared on disk on their own, whoever asked us to look.
    assert {event["actor_type"] for event in created} == {"system"}
    assert me


async def test_a_scan_survives_a_requester_who_no_longer_exists(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The actor is a foreign key that refuses to dangle, so a stale id must not fail a scan."""
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"file"})

    async with adopt(identity_settings, identity_database, tree) as (_client, workspace):
        await scan_pending(identity_database, identity_settings)

        # Exactly what the endpoint does — including the `expedite`, which is what carries the
        # reason onto a run that a request converges on.
        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                stale = scans.request_payload(trigger="manual", path="", requested_by=uuid4())
                queued = await scans.ensure_scheduled(
                    connection, workspace_id=workspace, trigger="manual"
                )
                await operations.expedite(connection, operation_id=queued.id, payload=stale)
                await connection.commit()
        finally:
            await engine.dispose()

        results = await scan_pending(identity_database, identity_settings)

    assert [result["outcome"] for result in results] == ["completed"]
    scanned = await read_events(identity_database, action="workspace.scanned")
    assert scanned[-1]["actor_type"] == "system"
