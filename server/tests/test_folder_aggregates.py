"""What a folder holds, and how long it takes to say so.

[F-015/FR-8](../../features/F-015-folders.md). Three properties carry the design, and every test
here is one of them:

- **the numbers converge on ground truth** — counted from the files themselves, not from a
  parallel tally that could be wrong in the same way twice;
- **a change is never applied twice and never lost**, because the batch that claims deltas and
  the addition that consumes them are one transaction;
- **a folder move and a rollup cannot interleave**, which is the one thing that could file a
  change against a tree that no longer exists.

The direct count gets its own attention for the opposite reason: it is exact *before* any rollup
runs, and a test that only ever asserted the settled state would not notice if it stopped being.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import aggregates, files, folders, handlers, janitor, names, operations
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.events import Actor
from store_everything.runner import PermanentFailureError
from store_everything.tables import folder as folder_table
from store_everything.tables import folder_aggregate, folder_delta, operation
from tests.identity_helpers import SAME_ORIGIN
from tests.test_reconciliation import rescan
from tests.test_scanning import adopt, build
from tests.upload_helpers import create_upload
from tests.workspace_helpers import (
    create_workspace,
    provision_pending,
    rollup_pending,
    run_pending,
    scan_pending,
    workspace_ready,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TREE: dict[str, bytes] = {
    "notes.txt": b"a plain file",
    "Album/beach.jpg": b"a photo",
    "Album/2026/party.jpg": b"another photo, longer",
    "Album/2026/summer/lake.jpg": b"deeper still",
}


def folder_url(folder_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/folders/{folder_id}"


async def make_folder(
    client: httpx.AsyncClient, workspace: UUID, name: str, *, parent: UUID | None = None
) -> UUID:
    body: dict[str, Any] = {"name": name}
    if parent is not None:
        body["parent"] = str(parent)
    response = await client.post(
        f"{API_V1_PREFIX}/workspaces/{workspace}/folders", json=body, headers=SAME_ORIGIN
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def reported(client: httpx.AsyncClient, folder_id: UUID) -> dict[str, Any]:
    """The `aggregates` object from a folder read — what a client actually sees."""
    response = await client.get(folder_url(folder_id))
    assert response.status_code == 200, response.text
    return dict(response.json()["aggregates"])


async def every_folder_matches_ground_truth(database_url: str) -> None:
    """The assertion this whole feature exists to satisfy, for every folder at once."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            identifiers = list((await connection.execute(select(folder_table.c.id))).scalars())
            truth = await aggregates.ground_truth(connection, identifiers)
            held = {
                row[0]: (row[1], row[2])
                for row in await connection.execute(
                    select(
                        folder_aggregate.c.folder_id,
                        folder_aggregate.c.total_files,
                        folder_aggregate.c.total_bytes,
                    )
                )
            }
            assert held == {
                identifier: truth.get(identifier, (0, 0)) for identifier in identifiers
            }, "a folder's stored totals disagree with the files underneath it"
    finally:
        await engine.dispose()


async def queued_deltas(database_url: str) -> list[tuple[UUID, UUID, int, int]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return [
                (row[0], row[1], row[2], row[3])
                for row in await connection.execute(
                    select(
                        folder_delta.c.workspace_id,
                        folder_delta.c.folder_id,
                        folder_delta.c.file_count,
                        folder_delta.c.size_bytes,
                    ).order_by(folder_delta.c.id)
                )
            ]
    finally:
        await engine.dispose()


async def folder_at(database_url: str, workspace: UUID, path: str) -> folders.Folder:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            found = await folders.resolve(
                connection, workspace_id=workspace, segments=names.split_path(path) if path else ()
            )
            assert found is not None, f"no folder at {path!r}"
            return found
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------- the two guarantees


@pytest.mark.fr("F-015/FR-8")
async def test_the_direct_count_is_exact_before_any_rollup_has_run(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The design's sharpest edge: one number is a count, the other two are a queue.

    A client that has just uploaded something sees it in `direct_files` immediately, learns from
    `pending` that the totals have not caught up, and can poll instead of guessing.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        uploaded = await create_upload(client, workspace, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text

        immediately = await reported(client, album)
        assert immediately["direct_files"] == 1, "the direct count is counted, not queued"
        assert immediately["total_files"] == 0, "nothing has drained the queue yet"
        assert immediately["total_bytes"] == 0
        assert immediately["pending"] is True

        await rollup_pending(identity_database, identity_settings)

        settled = await reported(client, album)
        assert settled["direct_files"] == 1
        assert settled["total_files"] == 1
        assert settled["total_bytes"] == len(b"a photo")
        assert settled["pending"] is False
        assert settled["as_of"] >= immediately["as_of"], "a settled drain advances the watermark"


@pytest.mark.fr("F-015/FR-8")
async def test_an_upload_reaches_every_ancestor_and_nothing_else(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """One delta, expanded over the closure — so the root counts what a leaf received."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        root = await folder_at(identity_database, workspace, "")
        album = await make_folder(client, workspace, "Album")
        year = await make_folder(client, workspace, "2026", parent=album)
        elsewhere = await make_folder(client, workspace, "Documents")

        uploaded = await create_upload(
            client, workspace, "Album/2026/party.jpg", body=b"a photo of a party"
        )
        assert uploaded.status_code == 201, uploaded.text
        await rollup_pending(identity_database, identity_settings)

        assert (await reported(client, year))["total_files"] == 1
        assert (await reported(client, album))["total_files"] == 1
        assert (await reported(client, root.id))["total_files"] == 1
        assert (await reported(client, elsewhere))["total_files"] == 0, (
            "a sibling subtree holds none of it"
        )

        # The direct counts say where the file actually is, which the recursive ones cannot.
        assert (await reported(client, year))["direct_files"] == 1
        assert (await reported(client, album))["direct_files"] == 0
        assert (await reported(client, root.id))["direct_files"] == 0

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8")
async def test_pending_is_about_this_folder_rather_than_the_workspace(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A folder nothing has changed is not stale because something else in the workspace is."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        documents = await make_folder(client, workspace, "Documents")
        await rollup_pending(identity_database, identity_settings)

        uploaded = await create_upload(client, workspace, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text

        assert (await reported(client, album))["pending"] is True
        assert (await reported(client, documents))["pending"] is False
        root = await folder_at(identity_database, workspace, "")
        assert (await reported(client, root.id))["pending"] is True, "an ancestor is affected too"


@pytest.mark.fr("F-015/FR-8")
async def test_a_thousand_files_converge_and_are_coalesced_into_one_row_each(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """[F-015/AC-7](../../features/F-015-folders.md): a thousand files into a deep folder.

    Also the coalescing claim, which is what makes the queue affordable: one drain writes one row
    per *affected folder*, not one per file. Five folders and a thousand deltas mean five updates.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        year = await make_folder(client, workspace, "2026", parent=album)
        summer = await make_folder(client, workspace, "summer", parent=year)

        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                for index in range(1000):
                    await files.register(
                        connection,
                        workspace_id=workspace,
                        folder_id=summer,
                        name=f"photo-{index:04}.jpg",
                        content_hash=f"{index:064x}",
                        size_bytes=index,
                        media_type="image/jpeg",
                        modified_at=None,
                        origin="upload",
                        actor=Actor.system(),
                    )
                await aggregates.schedule(connection, workspace)
                await connection.commit()
        finally:
            await engine.dispose()

        results = await rollup_pending(identity_database, identity_settings)

        # root, Album, 2026, summer — and nothing else, however many files contributed.
        assert [result["folders_updated"] for result in results] == [4]
        assert [result["batches"] for result in results] == [1], "a thousand deltas is one batch"

        totals = await reported(client, summer)
        assert totals["total_files"] == 1000
        assert totals["total_bytes"] == sum(range(1000))
        assert (await reported(client, album))["total_files"] == 1000
        assert (await reported(client, summer))["pending"] is False

    await every_folder_matches_ground_truth(identity_database)


# ------------------------------------------------------------------------- exactly once


@pytest.mark.fr("F-015/FR-8")
async def test_an_abandoned_drain_leaves_the_queue_untouched(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The whole reason the claim and the addition are one statement.

    A drain that does not commit must leave the deltas exactly as they were — not half applied,
    and not consumed. This rolls back where a crash would simply stop, which is the same thing
    from the database's point of view.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        uploaded = await create_upload(client, workspace, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text

        before = await queued_deltas(identity_database)
        assert before, "the upload queued a delta"

        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                await aggregates.lock(connection, workspace)
                assert await aggregates.drain(connection, workspace_id=workspace) > 0
                await connection.rollback()
        finally:
            await engine.dispose()

        assert await queued_deltas(identity_database) == before, "the deltas came back"
        assert (await reported(client, album))["total_files"] == 0, "and nothing was applied"

        await rollup_pending(identity_database, identity_settings)
        assert (await reported(client, album))["total_files"] == 1, "exactly once, on the retry"

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8")
async def test_a_total_the_arithmetic_cannot_reach_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A negative count is a bug, and the database says so rather than serving it.

    The rollup then fails, retries and eventually dead-letters — loudly, which is what this
    system does with code that is wrong. The alternative, clamping, would hide the bug *and*
    corrupt every later addition.
    """
    async with workspace_ready(identity_settings, identity_database) as (_client, workspace, _):
        empty = (await folder_at(identity_database, workspace, "")).id

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await aggregates.record(connection, workspace_id=workspace, folder_id=empty, files=-1)
            await connection.commit()
        async with engine.connect() as connection:
            await aggregates.lock(connection, workspace)
            with pytest.raises(IntegrityError):
                await aggregates.drain(connection, workspace_id=workspace)
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------- moves


@pytest.mark.fr("F-015/FR-8", "F-010/FR-1")
async def test_moving_a_file_leaves_the_ancestors_it_shares_alone(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Two deltas that cancel above the common ancestor — no special case for it."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        root = await folder_at(identity_database, workspace, "")
        album = await make_folder(client, workspace, "Album")
        documents = await make_folder(client, workspace, "Documents")
        uploaded = await create_upload(client, workspace, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text
        await rollup_pending(identity_database, identity_settings)

        before = await reported(client, root.id)
        moved = await client.post(
            f"{API_V1_PREFIX}/files/{uploaded.json()['id']}/move",
            json={"folder": str(documents)},
            headers=SAME_ORIGIN,
        )
        assert moved.status_code == 200, moved.text
        await rollup_pending(identity_database, identity_settings)

        assert (await reported(client, album))["total_files"] == 0
        assert (await reported(client, documents))["total_files"] == 1
        after_root = await reported(client, root.id)
        assert (
            after_root["total_files"],
            after_root["total_bytes"],
            after_root["direct_files"],
        ) == (
            before["total_files"],
            before["total_bytes"],
            before["direct_files"],
        ), "the root held the file before and after, so its numbers must not have moved"

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8", "F-015/FR-4")
async def test_moving_a_folder_carries_its_subtree_total_in_one_step(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """FR-8's O(depth): the subtree's own numbers do not move, and nothing counts its files."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        year = await make_folder(client, workspace, "2026", parent=album)
        archive = await make_folder(client, workspace, "Archive")
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            uploaded = await create_upload(
                client, workspace, f"Album/2026/{name}", body=name.encode()
            )
            assert uploaded.status_code == 201, uploaded.text
        await rollup_pending(identity_database, identity_settings)

        subtree_before = await reported(client, year)
        assert subtree_before["total_files"] == 3

        moved = await client.post(
            f"{folder_url(year)}/move", json={"parent": str(archive)}, headers=SAME_ORIGIN
        )
        assert moved.status_code == 200, moved.text
        results = await rollup_pending(identity_database, identity_settings)

        # Album and the root on the way out, Archive and the root on the way in: the root is one
        # row either way, and none of the three files produced a delta of its own.
        assert results[0]["folders_updated"] == 3

        assert (await reported(client, album))["total_files"] == 0
        assert (await reported(client, archive))["total_files"] == 3
        after = await reported(client, year)
        assert (after["total_files"], after["total_bytes"]) == (
            subtree_before["total_files"],
            subtree_before["total_bytes"],
        ), "the moved folder holds what it held; only its ancestors changed"

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8")
async def test_a_change_queued_before_a_move_lands_where_the_file_now_lives(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The interleaving the workspace lock exists for, in the order that is easy to get wrong.

    The upload's delta is queued while the folder is under `Album`, and drained after it has moved
    under `Archive`. It must reach `Archive` and must not leave anything behind in `Album` — which
    holds because the move shifted the *cached* total, and the cached total did not include it.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        year = await make_folder(client, workspace, "2026", parent=album)
        archive = await make_folder(client, workspace, "Archive")

        uploaded = await create_upload(client, workspace, "Album/2026/party.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text
        # No rollup here: the delta is deliberately still in the queue.

        moved = await client.post(
            f"{folder_url(year)}/move", json={"parent": str(archive)}, headers=SAME_ORIGIN
        )
        assert moved.status_code == 200, moved.text
        await rollup_pending(identity_database, identity_settings)

        assert (await reported(client, archive))["total_files"] == 1
        assert (await reported(client, album))["total_files"] == 0
        assert (await reported(client, year))["total_files"] == 1

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8", "F-015/FR-4")
async def test_a_cross_workspace_move_takes_its_queued_changes_with_it(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A delta filed under the source workspace names a folder the destination now owns.

    Left alone, the source's drain would update the destination's folders while holding the wrong
    lock. The move re-tags them, so each workspace's rollup only ever touches its own tree.
    """
    async with workspace_ready(identity_settings, identity_database, name="Source") as (
        client,
        source,
        _root,
    ):
        created = await create_workspace(client, "Archive")
        assert created.status_code == 201, created.text
        destination = UUID(created.json()["id"])
        await provision_pending(identity_database)

        album = await make_folder(client, source, "Album")
        year = await make_folder(client, source, "2026", parent=album)
        settled = await create_upload(client, source, "Album/2026/party.jpg", body=b"a photo")
        assert settled.status_code == 201, settled.text
        # Drained, so what is left in the queue below is only the second upload's delta.
        await rollup_pending(identity_database, identity_settings)

        queued = await create_upload(client, source, "Album/2026/extra.jpg", body=b"one more")
        assert queued.status_code == 201, queued.text

        destination_root = await folder_at(identity_database, destination, "")
        moved = await client.post(
            f"{folder_url(year)}/move",
            json={"parent": str(destination_root.id)},
            headers=SAME_ORIGIN,
        )
        assert moved.status_code == 200, moved.text

        await no_delta_names_another_workspaces_folder(identity_database)
        await rollup_pending(identity_database, identity_settings)

        assert (await reported(client, destination_root.id))["total_files"] == 2
        assert (await reported(client, year))["total_files"] == 2
        assert (await reported(client, album))["total_files"] == 0
        source_root = await folder_at(identity_database, source, "")
        assert (await reported(client, source_root.id))["total_files"] == 0

    await every_folder_matches_ground_truth(identity_database)


async def no_delta_names_another_workspaces_folder(database_url: str) -> None:
    """Every queued delta belongs to the rollup of the workspace that owns its folder.

    The invariant the re-tag exists for: a drain claims one workspace's rows while holding one
    workspace's lock, so a row pointing into another workspace's tree would be an unsynchronised
    write to it."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            mismatched = (
                await connection.execute(
                    select(func.count())
                    .select_from(
                        folder_delta.join(
                            folder_table, folder_table.c.id == folder_delta.c.folder_id
                        )
                    )
                    .where(folder_delta.c.workspace_id != folder_table.c.workspace_id)
                )
            ).scalar_one()
            assert mismatched == 0, f"{mismatched} queued delta(s) name another workspace's folder"
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------- the scan's changes


@pytest.mark.fr("F-015/FR-8")
async def test_a_deletion_and_a_reappearance_move_the_totals_both_ways(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The totals follow what the storage actually holds, not what was once uploaded."""
    tree = tmp_path / "nas"
    contents = build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        assert (await reported(client, root.id))["total_files"] == len(contents)

        (tree / "Album/2026/party.jpg").unlink()
        await rescan(client, workspace, identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)

        year = await folder_at(identity_database, workspace, "Album/2026")
        assert (await reported(client, year.id))["total_files"] == 1, "one left under 2026"
        assert (await reported(client, root.id))["total_files"] == len(contents) - 1
        await every_folder_matches_ground_truth(identity_database)

        (tree / "Album/2026/party.jpg").write_bytes(contents["Album/2026/party.jpg"])
        await rescan(client, workspace, identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)

        assert (await reported(client, root.id))["total_files"] == len(contents)

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8")
async def test_content_changed_on_the_storage_moves_only_the_difference(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A new version is not a new file: the count holds still and the bytes move by the delta."""
    tree = tmp_path / "nas"
    build(tree, {"Album/beach.jpg": b"a photo"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        assert (await reported(client, album.id))["total_bytes"] == len(b"a photo")

        (tree / "Album/beach.jpg").write_bytes(b"a much longer photo than before")
        await rescan(client, workspace, identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)

        totals = await reported(client, album.id)
        assert totals["total_files"] == 1, "still one file"
        assert totals["total_bytes"] == len(b"a much longer photo than before")

    await every_folder_matches_ground_truth(identity_database)


# ------------------------------------------------------------------------- the drift sweep


@pytest.mark.fr("F-015/FR-8")
async def test_the_sweep_corrects_a_total_that_disagrees_with_the_files(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """[F-015/AC-7](../../features/F-015-folders.md)'s second half, and then some.

    FR-8 asks for drift to be flagged; a warning is that flag. Correcting it as well is the part
    worth asserting: ground truth is authoritative, the folder has nothing queued, and serving a
    number known to be wrong helps nobody.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        uploaded = await create_upload(client, workspace, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text
        await rollup_pending(identity_database, identity_settings)

        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    update(folder_aggregate)
                    .where(folder_aggregate.c.folder_id == album)
                    .values(total_files=99, total_bytes=1, verified_at=None)
                )
                # Nothing changed, so nothing armed a rollup — which is exactly why the janitor
                # arms one per workspace on its hourly pass.
                await aggregates.schedule(connection, workspace)
                await connection.commit()
        finally:
            await engine.dispose()

        assert (await reported(client, album))["total_files"] == 99, "corrupted on purpose"

        results = await rollup_pending(identity_database, identity_settings)

        assert sum(result["drift_corrected"] for result in results) == 1
        corrected = await reported(client, album)
        assert (corrected["total_files"], corrected["total_bytes"]) == (1, len(b"a photo"))

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8")
async def test_the_sweep_leaves_a_folder_with_queued_changes_alone(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """For a folder with a delta waiting, a difference is lag — and "correcting" it would
    double-count the change still to come."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        uploaded = await create_upload(client, workspace, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text

        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                # The delta is still queued, so `album`'s stored zero is behind rather than wrong.
                drifted = await aggregates.verify(connection, workspace_id=workspace)
                assert drifted == [], "nothing here has settled yet"
                unverified = (
                    await connection.execute(
                        select(func.count())
                        .select_from(folder_aggregate)
                        .where(folder_aggregate.c.verified_at.is_(None))
                    )
                ).scalar_one()
                assert unverified == 2, "neither the root nor Album was stamped as checked"
        finally:
            await engine.dispose()

        await rollup_pending(identity_database, identity_settings)
        assert (await reported(client, album))["total_files"] == 1


# ------------------------------------------------------------------------- the plumbing


@pytest.mark.fr("F-015/FR-8")
async def test_every_folder_is_born_with_a_row_of_zeros(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Both ways a folder comes into existence — asked for, and found on the storage.

    A folder without a row would have its deltas land in an invented one holding the delta rather
    than the total, so this is the guard for any future third way of creating one.
    """
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/folders",
            json={"name": "Documents", "parent": str(root.id)},
            headers=SAME_ORIGIN,
        )

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    select(
                        select(func.count()).select_from(folder_table).scalar_subquery(),
                        select(func.count()).select_from(folder_aggregate).scalar_subquery(),
                    )
                )
            ).one()
            assert counts[0] == counts[1], "every folder has exactly one aggregate row"
    finally:
        await engine.dispose()


@pytest.mark.fr("F-015/FR-8")
async def test_the_janitor_arms_a_rollup_for_a_workspace_nothing_has_changed(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The drift sweep rides the rollup, and the rollup is armed by changes — so a quiet
    workspace needs somebody else to ask. That is the janitor's hourly pass (12 § the janitor)."""
    async with workspace_ready(identity_settings, identity_database) as (_client, _workspace, _):
        await rollup_pending(identity_database, identity_settings)
        assert await queued_rollups(identity_database) == 0, "nothing to do, nothing queued"

        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                # What `handlers.install_schedules` does on every worker start-up.
                await operations.ensure_scheduled(connection, kind=janitor.KIND, max_attempts=3)
                await connection.commit()
        finally:
            await engine.dispose()

        swept = await run_pending(
            identity_database,
            {janitor.KIND: handlers.registry(identity_settings)[janitor.KIND]},
        )
        assert [result["rollups_armed"] for result in swept] == [1]
        assert await queued_rollups(identity_database) == 1

        # And it really runs: the sweep it carries is the only thing that verifies this
        # workspace's folders while nothing is happening to them.
        results = await rollup_pending(identity_database, identity_settings)
        assert [result["settled"] for result in results] == [True]


async def queued_rollups(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (
                await connection.execute(
                    select(func.count())
                    .select_from(operation)
                    .where(operation.c.kind == aggregates.KIND, operation.c.state == "queued")
                )
            ).scalar_one()
    finally:
        await engine.dispose()


@pytest.mark.fr("F-015/FR-8")
async def test_a_rollup_needs_a_workspace_to_roll_up(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A kind whose subject is a workspace, enqueued without one, is a bug rather than a retry."""
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await operations.enqueue(connection, kind=aggregates.KIND, max_attempts=1)
            await connection.commit()
        with pytest.raises(PermanentFailureError):
            await rollup_pending(identity_database, identity_settings)
    finally:
        await engine.dispose()


@pytest.mark.fr("F-015/FR-8")
async def test_a_backlog_re_arms_instead_of_holding_the_lock(
    identity_settings: Settings,
    identity_database: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run drains a bounded amount and then asks for another.

    The bound is what keeps a folder move from waiting behind an import: the workspace lock is
    released with each batch's commit, and a run that stops short says so rather than reporting a
    settled queue. The real numbers are far larger, so the test shrinks them.
    """
    monkeypatch.setattr(aggregates, "BATCH", 1)
    monkeypatch.setattr(aggregates, "BATCHES_PER_RUN", 2)

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        album = await make_folder(client, workspace, "Album")
        for index in range(3):
            uploaded = await create_upload(
                client, workspace, f"Album/{index}.jpg", body=b"x" * (index + 1)
            )
            assert uploaded.status_code == 201, uploaded.text

        results = await rollup_pending(identity_database, identity_settings)

        assert [result["batches"] for result in results] == [2, 1], "two, then the last one"
        assert [result["settled"] for result in results] == [False, True]
        assert (await reported(client, album))["total_files"] == 3

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-8")
async def test_nothing_is_queued_for_a_change_of_nothing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Two deliberate no-ops, because both are on paths that see a lot of traffic.

    A rename that stays in its folder changes no total, and a caller asking about no folders is
    asking nothing — neither should cost a row or a round trip."""
    async with workspace_ready(identity_settings, identity_database) as (_client, workspace, _):
        root = (await folder_at(identity_database, workspace, "")).id

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await aggregates.record(connection, workspace_id=workspace, folder_id=root)
            assert await aggregates.ground_truth(connection, []) == {}
            await connection.commit()
    finally:
        await engine.dispose()

    assert await queued_deltas(identity_database) == []
