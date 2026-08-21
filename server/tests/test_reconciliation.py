"""What a re-scan does about files that changed, moved or vanished on the storage.

[F-001/FR-6](../../features/F-001-upload-and-import.md) in one file. The assertions come in
pairs again, and here the second half of each pair is the important one: the *dangerous*
direction is concluding that files were deleted when the storage was merely unreachable, so
every "it trashed the file that was gone" is matched by a "it left the files it could not look
at alone".

Nothing in this module touches the source tree. The scan reads it; the tests write it, exactly
the way a user copying files onto a NAS over SMB does.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import files, workspacefs
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.tables import file, file_version, scan_blocked, trash_entry
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.test_scanning import adopt, build, registered, status, user_files
from tests.workspace_helpers import scan_pending

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Distinct content per path, so a hash identifies a file unambiguously unless a test wants
#: otherwise.
TREE: dict[str, bytes] = {
    "notes.txt": b"the original note",
    "Photos/beach.jpg": b"pretend this is a photo",
    "Photos/2026/party.jpg": b"another photo entirely",
    "Documents/tax/return.pdf": b"%PDF-1.7 pretend",
}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def rescan(client: Any, workspace: UUID, database_url: str, settings: Settings) -> None:
    """Ask for a rescan the way a user does, then let the worker run it."""
    response = await client.post(
        f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
    )
    assert response.status_code == 202, response.text
    await scan_pending(database_url, settings)


async def summary(client: Any, file_id: UUID) -> dict[str, Any]:
    response = await client.get(f"{API_V1_PREFIX}/files/{file_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def identify(database_url: str, path: str) -> UUID:
    """The UUID of the **live** file at this path — the thing a move has to preserve.

    Live specifically: a trashed row keeps sitting at the path it used to hold, so a path can
    legitimately have two rows once something new arrives there (F-014/FR-1), and only one of
    them is the file anyone can open.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            for file_id in (
                (await connection.execute(select(file.c.id).where(file.c.state == "live")))
                .scalars()
                .all()
            ):
                found = await files.get(connection, file_id)
                assert found is not None
                if await files.path_of(connection, found) == path:
                    return found.id
            raise AssertionError(f"no live file is registered at {path!r}")
    finally:
        await engine.dispose()


async def states(database_url: str) -> dict[str, str]:
    """Every file by path and lifecycle state, trashed rows included."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            found: dict[str, str] = {}
            for file_id in (await connection.execute(select(file.c.id))).scalars().all():
                record = await files.get(connection, file_id)
                assert record is not None
                found[await files.path_of(connection, record)] = record.state
            return found
    finally:
        await engine.dispose()


async def versions_of(database_url: str, file_id: UUID) -> list[dict[str, Any]]:
    """A file's versions, oldest first, as the rows say."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        file_version.c.content_hash,
                        file_version.c.size_bytes,
                        file_version.c.origin,
                        file_version.c.is_current,
                        file_version.c.restorable,
                    )
                    .where(file_version.c.file_id == file_id)
                    .order_by(file_version.c.created_at)
                )
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


async def trash_rows(database_url: str) -> list[dict[str, Any]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        trash_entry.c.file_id,
                        trash_entry.c.origin,
                        trash_entry.c.batch_id,
                        trash_entry.c.path,
                        trash_entry.c.trashed_at,
                        trash_entry.c.trashed_by,
                        trash_entry.c.purge_after,
                    ).order_by(trash_entry.c.path)
                )
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


async def blocked(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (
                await connection.execute(select(func.count()).select_from(scan_blocked))
            ).scalar_one()
    finally:
        await engine.dispose()


async def imported(
    settings: Settings, database_url: str, tree: Path, contents: dict[str, bytes] | None = None
) -> Any:
    """Build a tree, adopt it and run the initial import. Returns the open context."""
    # This module's own tree by default — distinct content per path, so a test that wants
    # ambiguity has to ask for it.
    build(tree, TREE if contents is None else contents)
    return adopt(settings, database_url, tree)


# ---------------------------------------------------------------- content that changed


@pytest.mark.fr("F-001/FR-6", "F-007/FR-1")
async def test_content_changed_on_the_storage_becomes_a_new_version(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-4's middle clause: an externally modified file yields a new version."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "notes.txt")

        (tree / "notes.txt").write_bytes(b"edited straight on the NAS")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)
        current = await summary(client, file_id)

    history = await versions_of(identity_database, file_id)
    assert [row["is_current"] for row in history] == [False, True]
    assert [row["content_hash"] for row in history] == [
        digest(TREE["notes.txt"]),
        digest(b"edited straight on the NAS"),
    ]
    assert [row["origin"] for row in history] == ["external", "external"]
    # F-007/FR-9: the app never had a chance to copy the old bytes, and says so rather than
    # promising a restore that would fail.
    assert history[0]["restorable"] is False
    assert history[1]["restorable"] is True

    # The file kept its identity, and the current version is what the API reports.
    assert current["id"] == str(file_id)
    assert current["content_hash"] == digest(b"edited straight on the NAS")
    assert report["recent"][0]["files_changed"] == 1
    assert report["recent"][0]["files_trashed"] == 0
    assert await trash_rows(identity_database) == []


@pytest.mark.fr("F-007/FR-9")
async def test_a_predecessor_the_app_still_holds_stays_restorable(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Restorability is asked, not assumed.

    If the blob store happens to hold the superseded content — an earlier app-mediated write
    snapshotted exactly these bytes — then history really is intact, and reporting
    `restorable: false` would be a lie in the safe direction rather than the truth.
    """
    tree = tmp_path / "nas"
    store = BlobStore(identity_settings.versions_root)
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "notes.txt")
        store.put_bytes(TREE["notes.txt"], operation_id=workspace)

        (tree / "notes.txt").write_bytes(b"a later edit on the storage")
        await rescan(client, workspace, identity_database, identity_settings)

    history = await versions_of(identity_database, file_id)
    assert history[0]["restorable"] is True, "the bytes are in the store, so it can be restored"


async def test_a_touched_file_is_not_a_new_version(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Same bytes, new timestamp: one version, and the recorded timestamp follows the disk.

    Without the refresh the file would be re-read on every pass forever, which at 10 TB is the
    difference between a stat-scan and re-hashing the world.
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "notes.txt")

        later = datetime.now(tz=UTC) + timedelta(hours=1)
        os.utime(tree / "notes.txt", (later.timestamp(), later.timestamp()))
        await rescan(client, workspace, identity_database, identity_settings)
        first = await status(client, workspace)
        current = await summary(client, file_id)

        # And the pass after it has nothing left to notice.
        await rescan(client, workspace, identity_database, identity_settings)
        second = await status(client, workspace)

    assert len(await versions_of(identity_database, file_id)) == 1
    assert first["recent"][0]["files_changed"] == 0
    assert second["recent"][0]["files_changed"] == 0
    assert current["modified_at"] is not None
    assert datetime.fromisoformat(current["modified_at"]).hour == later.hour


# ------------------------------------------------------------------- files that vanished


@pytest.mark.fr("F-001/FR-6", "F-014/FR-10")
async def test_a_file_deleted_on_the_storage_becomes_a_trash_entry(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-4's last clause: never silently dropped from the index."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "Photos/beach.jpg")

        (tree / "Photos/beach.jpg").unlink()
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)
        gone = await summary(client, file_id)
        content = await client.get(f"{API_V1_PREFIX}/files/{file_id}/content")

    entries = await trash_rows(identity_database)
    assert len(entries) == 1
    assert entries[0]["file_id"] == file_id
    assert entries[0]["origin"] == "detected_on_disk"
    assert entries[0]["path"] == "Photos/beach.jpg"
    # Nobody deleted it, so nobody is named (07 § the audit trail).
    assert entries[0]["trashed_by"] is None
    # F-014/FR-6: the deadline is stored, and it is the instance default away.
    assert entries[0]["purge_after"] - entries[0]["trashed_at"] == timedelta(days=30)
    # The batch is the run, so "put back everything that scan removed" is one call later.
    assert entries[0]["batch_id"] == UUID(report["recent"][0]["id"])

    assert gone["state"] == "trashed"
    assert gone["trash"]["origin"] == "detected_on_disk"
    assert gone["trash"]["restorable"] is False, "its bytes were on the storage and are gone"
    assert content.status_code == 410

    assert report["recent"][0]["files_trashed"] == 1
    trashed_events = await read_events(identity_database, action="file.trashed")
    assert [event["actor_type"] for event in trashed_events] == ["system"]
    assert trashed_events[0]["details"]["origin"] == "detected_on_disk"

    # The other three are untouched, and the row is still there to be restored from.
    assert await states(identity_database) == {
        "notes.txt": "live",
        "Photos/beach.jpg": "trashed",
        "Photos/2026/party.jpg": "live",
        "Documents/tax/return.pdf": "live",
    }


async def test_a_deleted_subtree_trashes_everything_under_it(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A directory removed on the storage is noticed at every depth below it.

    The frontier never hears about the vanished directory again — its parent stops listing it —
    so this only works because the sweep asks the subtree, not the directories it walked.
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        shutil.rmtree(tree / "Photos")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    assert await states(identity_database) == {
        "notes.txt": "live",
        "Photos/beach.jpg": "trashed",
        "Photos/2026/party.jpg": "trashed",
        "Documents/tax/return.pdf": "live",
    }
    assert report["recent"][0]["files_trashed"] == 2


@pytest.mark.fr("F-014/FR-10")
async def test_content_that_reappears_at_its_own_path_is_reactivated(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The self-healing half of FR-10, and why an over-eager trash entry is survivable.

    A share that half-mounted and came back must not leave a second identity behind: the same
    row returns, with its history.
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "Photos/beach.jpg")

        (tree / "Photos/beach.jpg").unlink()
        await rescan(client, workspace, identity_database, identity_settings)
        (tree / "Photos/beach.jpg").write_bytes(TREE["Photos/beach.jpg"])
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)
        back = await summary(client, file_id)

    assert back["state"] == "live"
    assert back["trash"] is None
    assert await trash_rows(identity_database) == []
    assert len(await versions_of(identity_database, file_id)) == 1, "no new version, no new file"
    assert await identify(identity_database, "Photos/beach.jpg") == file_id
    assert report["recent"][0]["files_restored"] == 1
    restored = await read_events(identity_database, action="file.restored")
    assert restored[0]["details"]["reason"] == "the content reappeared on the storage"


async def test_different_content_at_a_trashed_path_is_a_new_file(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """FR-10 reactivates on the same hash. Anything else is a different file that happens to
    have the same name, and the trashed row keeps its own history rather than inheriting one."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        original = await identify(identity_database, "notes.txt")

        (tree / "notes.txt").unlink()
        await rescan(client, workspace, identity_database, identity_settings)
        (tree / "notes.txt").write_bytes(b"a completely different note")
        await rescan(client, workspace, identity_database, identity_settings)

    replacement = await identify(identity_database, "notes.txt")
    assert replacement != original, "a different file took the name"
    assert len(await trash_rows(identity_database)) == 1
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            was = await files.get(connection, original)
            assert was is not None and was.state == "trashed"
    finally:
        await engine.dispose()


async def test_a_file_registered_after_the_run_started_is_not_trashed(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A file that arrived while the scan was walking is not missing — it was simply late.

    Simulated by forward-dating the row, which is exactly the state an upload that lands after
    the traversal passed its directory leaves behind. Scans are convergent, not
    snapshot-perfect: the next pass reconciles it.
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "notes.txt")

        (tree / "notes.txt").unlink()
        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    update(file)
                    .where(file.c.id == file_id)
                    .values(
                        created_at=text("now() + interval '1 hour'"),
                        last_seen_at=None,
                    )
                )
                await connection.commit()
        finally:
            await engine.dispose()

        await rescan(client, workspace, identity_database, identity_settings)

    assert await trash_rows(identity_database) == []
    assert (await states(identity_database))["notes.txt"] == "live"


# ------------------------------------------------------------------------ files that moved


@pytest.mark.fr("F-001/FR-19")
async def test_a_file_moved_on_the_storage_keeps_its_identity(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """One file, one new path, same UUID — and no trash entry anywhere near it."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        file_id = await identify(identity_database, "Photos/beach.jpg")

        (tree / "Photos/beach.jpg").rename(tree / "Documents/beach.jpg")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)
        moved = await summary(client, file_id)

    assert moved["path"] == "Documents/beach.jpg"
    assert moved["state"] == "live"
    assert await trash_rows(identity_database) == []
    assert len(await versions_of(identity_database, file_id)) == 1, "a move is not a new version"
    assert report["recent"][0]["files_moved"] == 1
    assert report["recent"][0]["files_registered"] == 0
    assert report["recent"][0]["files_trashed"] == 0

    events = await read_events(identity_database, action="file.moved")
    assert len(events) == 1
    assert events[0]["details"]["from"] == "Photos/beach.jpg"
    assert events[0]["details"]["to"] == "Documents/beach.jpg"
    assert events[0]["details"]["match"] == "hash"
    assert events[0]["actor_type"] == "system", "the scan discovered this; nobody did it"


@pytest.mark.fr("F-001/FR-19")
async def test_a_renamed_folder_moves_its_files_even_when_their_content_is_identical(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The case the hash alone cannot resolve, and the reason the name is the second rule.

    Three byte-identical files in one directory: every one of them is a candidate for every
    other, so matching on content alone is ambiguous three times over. Sibling names are unique
    within a folder, so preferring the candidate that kept its name recovers the whole rename —
    three moves, no deletions, no new identities.

    `a.jpg` is added a pass later than the other two on purpose, so registration order (b, c, a)
    disagrees with name order (a, b, c). Without the name rule the identities shuffle: the first
    entry visited would take the oldest row, and every file would come out carrying a different
    file's history.
    """
    tree = tmp_path / "nas"
    same = b"the very same bytes"
    contents = {"Album/b.jpg": same, "Album/c.jpg": same}
    async with await imported(identity_settings, identity_database, tree, contents) as (
        client,
        workspace,
    ):
        await scan_pending(identity_database, identity_settings)
        (tree / "Album/a.jpg").write_bytes(same)
        await rescan(client, workspace, identity_database, identity_settings)
        before = {name: await identify(identity_database, f"Album/{name}.jpg") for name in "abc"}

        (tree / "Album").rename(tree / "Album 2026")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    after = {name: await identify(identity_database, f"Album 2026/{name}.jpg") for name in "abc"}
    assert after == before, "a renamed folder replaced its files instead of moving them"
    assert await trash_rows(identity_database) == []
    assert report["recent"][0]["files_moved"] == 3
    assert report["recent"][0]["files_registered"] == 0
    # The name rule resolves the first two; by the third there is only one vanished file left
    # with that content, so the hash alone is unambiguous again.
    assert [
        event["details"]["match"]
        for event in await read_events(identity_database, action="file.moved")
    ] == ["hash+name", "hash+name", "hash"]


async def test_identical_files_that_both_vanish_still_move_deterministically(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Same content, same name, two vanished directories: the oldest registration wins.

    Arbitrary, and deliberately so — the candidates are indistinguishable to a person too. The
    choice is recorded in the event, and matching beats losing identity: the alternative is two
    trash entries and two new files for a pair of directories that were merely renamed.
    """
    tree = tmp_path / "nas"
    same = b"identical twins"
    contents = {"One/photo.jpg": same, "Two/photo.jpg": same}
    async with await imported(identity_settings, identity_database, tree, contents) as (
        client,
        workspace,
    ):
        await scan_pending(identity_database, identity_settings)
        oldest = await identify(identity_database, "One/photo.jpg")

        (tree / "One").rename(tree / "Alpha")
        (tree / "Two").rename(tree / "Beta")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    assert report["recent"][0]["files_moved"] == 2
    assert report["recent"][0]["files_registered"] == 0
    assert await trash_rows(identity_database) == []
    # `Alpha` is visited first, and takes the older of the two interchangeable identities.
    assert await identify(identity_database, "Alpha/photo.jpg") == oldest
    matches = [
        event["details"]["match"]
        for event in await read_events(identity_database, action="file.moved")
    ]
    assert matches == ["oldest-of-2", "hash"], "the second one is unambiguous by then"


async def test_a_copy_is_a_new_file_rather_than_a_move(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The negative case for the heuristic: the original is still there, so nothing moved."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        original = await identify(identity_database, "notes.txt")

        shutil.copy2(tree / "notes.txt", tree / "Documents/notes.txt")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    assert await identify(identity_database, "notes.txt") == original
    assert await identify(identity_database, "Documents/notes.txt") != original
    assert report["recent"][0]["files_registered"] == 1
    assert report["recent"][0]["files_moved"] == 0
    assert await read_events(identity_database, action="file.moved") == []


# -------------------------------------------------------------- what must never happen


@pytest.mark.fr("F-001/FR-16")
async def test_an_unreadable_directory_keeps_its_files(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-11, and the worst bug this feature could have.

    A directory the scan cannot read says **nothing** about what is inside it. Reconciling it as
    if everything in it had been deleted would trash the contents of a share whose permissions
    changed for a minute.
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        closed = tree / "Photos"
        os.chmod(closed, 0o000)
        try:
            if os.access(closed, os.R_OK):  # pragma: no cover - running as root
                pytest.skip("this process can read a mode-000 directory, so it cannot be tested")
            await rescan(client, workspace, identity_database, identity_settings)
            report = await status(client, workspace)
        finally:
            os.chmod(closed, 0o700)

    assert await trash_rows(identity_database) == [], "an unreadable directory was reconciled"
    assert await states(identity_database) == {
        "notes.txt": "live",
        "Photos/beach.jpg": "live",
        "Photos/2026/party.jpg": "live",
        "Documents/tax/return.pdf": "live",
    }
    # It was recorded as blocked, which is what excluded its subtree — including the nested
    # directory the scan never even reached.
    assert await blocked(identity_database) == 1
    assert report["recent"][0]["files_trashed"] == 0
    assert any("cannot be read" in finding["detail"] for finding in report["findings"]["data"])


@pytest.mark.fr("F-001/FR-17")
async def test_a_root_without_its_marker_is_not_reconciled(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The mount sentinel. An unmounted share looks exactly like an empty one.

    Without this check, one scheduled pass over a mount point that did not come back after a
    reboot would trash every file in the workspace. The run refuses instead, says why, and
    leaves the schedule armed so the share coming back is picked up on its own.
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        before = await registered(identity_database)

        # What an unmounted root looks like: the directory is there and holds nothing of ours.
        for entry in tree.iterdir():
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        assert not workspacefs.marker_path(tree).exists()

        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    assert await trash_rows(identity_database) == []
    assert await registered(identity_database) == before, "the index survived an empty mount"
    latest = report["recent"][0]
    assert latest["state"] == "failed"
    assert latest["files_trashed"] == 0
    assert "not there" in (latest["error"] or "")
    assert any("mount" in finding["detail"] for finding in report["findings"]["data"])


@pytest.mark.fr("F-001/FR-17")
async def test_a_root_carrying_another_workspaces_marker_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A mount that came back pointing somewhere else is not this workspace's storage."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        workspacefs.materialize(
            tree,
            workspace_id=UUID(int=1),
            placement="adopted",
            created_at=datetime.now(tz=UTC),
            operation_id=UUID(int=2),
        )
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    assert await trash_rows(identity_database) == []
    assert report["recent"][0]["state"] == "failed"
    assert "belongs to workspace" in (report["recent"][0]["error"] or "")


@pytest.mark.fr("F-001/FR-6")
async def test_a_subtree_rescan_never_trashes_anything_outside_it(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A rescan of one directory concludes nothing about the rest of the workspace."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        (tree / "notes.txt").unlink()
        (tree / "Photos/beach.jpg").unlink()
        response = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
            json={"path": "Photos"},
            headers=SAME_ORIGIN,
        )
        assert response.status_code == 202, response.text
        await scan_pending(identity_database, identity_settings)

    assert [entry["path"] for entry in await trash_rows(identity_database)] == ["Photos/beach.jpg"]
    assert (await states(identity_database))["notes.txt"] == "live"


@pytest.mark.fr("F-001/FR-18")
async def test_a_name_the_scan_refused_is_not_treated_as_missing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A registered file replaced by a symlink is *present*, however unusable.

    Only an absence concludes a deletion. The link is reported as skipped — the app will not
    follow it and will not serve it — but the file it shadows keeps its identity, because
    "there is something else at that name" is not "there is nothing at that name".
    """
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        (tree / "notes.txt").unlink()
        (tree / "notes.txt").symlink_to(tmp_path / "elsewhere.txt")
        await rescan(client, workspace, identity_database, identity_settings)
        report = await status(client, workspace)

    assert await trash_rows(identity_database) == []
    assert (await states(identity_database))["notes.txt"] == "live"
    assert any(
        finding["path"] == "notes.txt" and "symbolic" in finding["detail"]
        for finding in report["findings"]["data"]
    )


@pytest.mark.fr("F-001/FR-1", "F-001/FR-4")
async def test_reconciliation_never_touches_the_tree(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-9 for the re-scan path: every conclusion above is a row, never a write to the disk."""
    tree = tmp_path / "nas"
    async with await imported(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)

        (tree / "notes.txt").write_bytes(b"edited")
        (tree / "Photos/beach.jpg").unlink()
        (tree / "Photos/2026/party.jpg").rename(tree / "party.jpg")
        expected = user_files(tree)

        await rescan(client, workspace, identity_database, identity_settings)

    assert user_files(tree) == expected
