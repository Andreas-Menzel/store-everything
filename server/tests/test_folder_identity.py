"""A directory renamed on the storage is the same folder afterwards.

[F-015/FR-7](../../features/F-015-folders.md). To a scan, a rename looks like a deletion and a
creation: the old name is not in the listing and the new one is. The files inside already survive
that — matched by content, relocated, same UUIDs
([F-001/FR-19](../../features/F-001-upload-and-import.md)) — and this is the folder catching up
with them, because a subtree grant that evaporated when someone tidied their NAS would be worse
than no grant at all.

The tests come in three groups, and the middle one is the point:

- **it works** for a rename, a move to another parent, and a whole subtree at once;
- **it refuses** when the evidence does not single out one directory — a split, a merge, an empty
  folder, a destination that already existed, a subtree the scan could not read;
- **it leaves the numbers right**, because a transfer moves rows under a folder whose totals are
  maintained by a queue (F-015/FR-8) and that is where this could go quietly wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import folders, names
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.tables import folder as folder_table
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.test_folder_aggregates import every_folder_matches_ground_truth
from tests.test_reconciliation import rescan
from tests.test_scanning import adopt, build, registered
from tests.workspace_helpers import rollup_pending, scan_pending

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TREE: dict[str, bytes] = {
    "Album/beach.jpg": b"a photo of a beach",
    "Album/party.jpg": b"a photo of a party",
    "Album/2026/lake.jpg": b"a photo of a lake",
}


def folder_url(folder_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/folders/{folder_id}"


async def folder_at(database_url: str, workspace: UUID, path: str) -> folders.Folder | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await folders.resolve(
                connection,
                workspace_id=workspace,
                segments=names.split_path(path) if path else (),
            )
    finally:
        await engine.dispose()


async def folder_count(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (
                await connection.execute(select(func.count()).select_from(folder_table))
            ).scalar_one()
    finally:
        await engine.dispose()


async def summary(client: httpx.AsyncClient, folder_id: UUID) -> dict[str, Any]:
    response = await client.get(folder_url(folder_id))
    assert response.status_code == 200, response.text
    return dict(response.json())


async def latest_run(client: httpx.AsyncClient, workspace: UUID) -> dict[str, Any]:
    response = await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}/import-status")
    assert response.status_code == 200, response.text
    runs = response.json()["recent"]
    assert runs, "a scan has run"
    return dict(runs[0])


# ------------------------------------------------------------------------- it works


@pytest.mark.fr("F-015/FR-7", "F-015/FR-11")
async def test_a_renamed_directory_is_the_same_folder(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """[F-015/AC-2](../../features/F-015-folders.md): the id survives, and so does everything
    hanging off it. The files were never in doubt; the folder is what this adds."""
    tree = tmp_path / "nas"
    build(tree, {"Album/beach.jpg": b"a photo of a beach", "Album/party.jpg": b"another"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        before = await folder_at(identity_database, workspace, "Album")
        assert before is not None
        folders_before = await folder_count(identity_database)

        (tree / "Album").rename(tree / "Photos")
        await rescan(client, workspace, identity_database, identity_settings)

        after = await summary(client, before.id)
        assert after["id"] == str(before.id), "the folder kept its identity"
        assert after["name"] == "Photos"
        assert after["path"] == "Photos"
        assert await folder_count(identity_database) == folders_before, (
            "the row the traversal created for the new name was discarded, not kept beside it"
        )
        assert await folder_at(identity_database, workspace, "Album") is None

        # The files came along, still theirs, and their paths are derived from the folder chain.
        assert set(await registered(identity_database)) == {
            "Photos/beach.jpg",
            "Photos/party.jpg",
        }
        run = await latest_run(client, workspace)
        assert run["folders_transferred"] == 1
        assert run["folders_ambiguous"] == 0

        moved = await read_events(identity_database, action="folder.renamed")
        assert [event["details"]["detected"] for event in moved] == ["external"]
        assert moved[-1]["details"]["from"] == "Album"
        assert moved[-1]["details"]["to"] == "Photos"


@pytest.mark.fr("F-015/FR-7")
async def test_a_directory_moved_to_another_parent_is_the_same_folder(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A rename and a move differ only in whether the parent changed, and FR-7 covers both."""
    tree = tmp_path / "nas"
    build(tree, {"Album/beach.jpg": b"a photo", "Archive/.keep": b""})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        before = await folder_at(identity_database, workspace, "Album")
        assert before is not None

        (tree / "Album").rename(tree / "Archive" / "Album")
        await rescan(client, workspace, identity_database, identity_settings)

        after = await summary(client, before.id)
        assert after["id"] == str(before.id)
        assert after["path"] == "Archive/Album"
        assert after["depth"] == 2, "the depth followed the new position"
        assert set(await registered(identity_database)) == {
            "Archive/Album/beach.jpg",
            "Archive/.keep",
        }

        moved = await read_events(identity_database, action="folder.moved")
        assert [event["details"]["detected"] for event in moved] == ["external"]


@pytest.mark.fr("F-015/FR-7")
async def test_a_renamed_subtree_keeps_every_folder_it_contains(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The case the ordering exists for.

    Renaming a directory renames everything under it, so every folder in the subtree vanished and
    every one has a new counterpart. Processing a parent before its children would put two folders
    of the same name under one parent, which sibling uniqueness rightly refuses — so the pass goes
    deepest first.
    """
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        year = await folder_at(identity_database, workspace, "Album/2026")
        assert album is not None and year is not None
        folders_before = await folder_count(identity_database)

        (tree / "Album").rename(tree / "Photos")
        await rescan(client, workspace, identity_database, identity_settings)

        assert (await summary(client, album.id))["path"] == "Photos"
        assert (await summary(client, year.id))["path"] == "Photos/2026"
        assert await folder_count(identity_database) == folders_before
        assert set(await registered(identity_database)) == {
            "Photos/beach.jpg",
            "Photos/party.jpg",
            "Photos/2026/lake.jpg",
        }
        assert (await latest_run(client, workspace))["folders_transferred"] == 2


@pytest.mark.fr("F-015/FR-7")
async def test_a_directory_holding_only_subdirectories_keeps_its_identity(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Its files cannot vouch for it, because it has none. Its children can.

    A photo library organised by year is exactly this shape, and it is where a grant would live —
    so the folder at the top is the one that must not lose its id.
    """
    tree = tmp_path / "nas"
    build(tree, {"Library/2025/one.jpg": b"one", "Library/2026/two.jpg": b"two"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        library = await folder_at(identity_database, workspace, "Library")
        assert library is not None
        assert (await summary(client, library.id))["aggregates"]["direct_files"] == 0

        (tree / "Library").rename(tree / "Pictures")
        await rescan(client, workspace, identity_database, identity_settings)

        after = await summary(client, library.id)
        assert after["id"] == str(library.id), "the container folder kept its identity too"
        assert after["path"] == "Pictures"
        assert (await latest_run(client, workspace))["folders_transferred"] == 3


# ------------------------------------------------------------------------- it refuses


@pytest.mark.fr("F-015/FR-7")
async def test_an_empty_directory_renamed_gets_a_new_identity(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The documented limitation, asserted (AC-2). Nothing was inside it, so nothing can say the
    new directory is the same one — and inventing that link is how a grant ends up on the wrong
    folder."""
    tree = tmp_path / "nas"
    build(tree, {"Album/beach.jpg": b"a photo"})
    (tree / "Empty").mkdir()

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        empty = await folder_at(identity_database, workspace, "Empty")
        assert empty is not None

        (tree / "Empty").rename(tree / "Renamed")
        await rescan(client, workspace, identity_database, identity_settings)

        fresh = await folder_at(identity_database, workspace, "Renamed")
        assert fresh is not None
        assert fresh.id != empty.id, "a new identity, as documented"
        run = await latest_run(client, workspace)
        assert run["folders_transferred"] == 0
        assert run["folders_ambiguous"] == 0, "no evidence at all is not an ambiguous case"


@pytest.mark.fr("F-015/FR-7")
async def test_content_scattered_across_new_directories_is_ambiguous(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A split: no single directory holds a majority, so no directory inherits the identity."""
    tree = tmp_path / "nas"
    build(tree, {"Album/a.jpg": b"aaa", "Album/b.jpg": b"bbb", "Album/c.jpg": b"ccc"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        assert album is not None

        for name, destination in (("a.jpg", "One"), ("b.jpg", "Two"), ("c.jpg", "Three")):
            (tree / destination).mkdir()
            (tree / "Album" / name).rename(tree / destination / name)
        (tree / "Album").rmdir()
        await rescan(client, workspace, identity_database, identity_settings)

        assert (await summary(client, album.id))["path"] == "Album", (
            "the folder is untouched: nothing earned its identity"
        )
        run = await latest_run(client, workspace)
        assert run["folders_transferred"] == 0
        assert run["folders_ambiguous"] == 1

        logged = await read_events(identity_database, action="folder.identity_ambiguous")
        assert logged[-1]["details"]["reason"] == "split"
        assert len(logged[-1]["details"]["candidates"]) == 3


@pytest.mark.fr("F-015/FR-7")
async def test_two_directories_emptied_into_one_are_ambiguous(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A merge: one new directory holds a majority of *two* folders' content, and it cannot be
    both of them. FR-7 gives it to neither."""
    tree = tmp_path / "nas"
    build(tree, {"Album/a.jpg": b"aaa", "Documents/b.txt": b"bbb"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        documents = await folder_at(identity_database, workspace, "Documents")
        assert album is not None and documents is not None

        (tree / "Everything").mkdir()
        (tree / "Album" / "a.jpg").rename(tree / "Everything" / "a.jpg")
        (tree / "Documents" / "b.txt").rename(tree / "Everything" / "b.txt")
        (tree / "Album").rmdir()
        (tree / "Documents").rmdir()
        await rescan(client, workspace, identity_database, identity_settings)

        assert (await summary(client, album.id))["path"] == "Album"
        assert (await summary(client, documents.id))["path"] == "Documents"
        run = await latest_run(client, workspace)
        assert run["folders_transferred"] == 0
        assert run["folders_ambiguous"] == 2, "both claimants, not just the loser"

        logged = await read_events(identity_database, action="folder.identity_ambiguous")
        assert {event["details"]["reason"] for event in logged} == {"merge"}


@pytest.mark.fr("F-015/FR-7")
async def test_files_moved_into_a_folder_that_already_existed_is_not_a_rename(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A destination that was already there is a place files were moved *into*. Handing it another
    folder's identity would take the grants off a directory that never went anywhere."""
    tree = tmp_path / "nas"
    build(tree, {"Album/a.jpg": b"aaa", "Archive/keep.txt": b"kept"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        archive = await folder_at(identity_database, workspace, "Archive")
        assert album is not None and archive is not None

        (tree / "Album" / "a.jpg").rename(tree / "Archive" / "a.jpg")
        (tree / "Album").rmdir()
        await rescan(client, workspace, identity_database, identity_settings)

        assert (await summary(client, archive.id))["id"] == str(archive.id)
        assert (await summary(client, album.id))["path"] == "Album", "left alone"
        assert (await latest_run(client, workspace))["folders_transferred"] == 0
        assert set(await registered(identity_database)) == {
            "Archive/a.jpg",
            "Archive/keep.txt",
        }


@pytest.mark.fr("F-015/FR-7", "F-001/FR-16")
async def test_a_directory_the_scan_could_not_read_never_looks_renamed(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """ "I could not look" is not "it is not there" — here the cost of confusing them would be
    handing a live folder's identity to another directory."""
    tree = tmp_path / "nas"
    build(tree, {"Album/2026/lake.jpg": b"a photo", "Other/keep.txt": b"kept"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        year = await folder_at(identity_database, workspace, "Album/2026")
        assert year is not None

        # The subtree is unreadable, so the run learns nothing about `2026` at all — while a copy
        # of its content turns up somewhere the run *can* read.
        (tree / "Elsewhere").mkdir()
        (tree / "Elsewhere" / "lake.jpg").write_bytes(b"a photo")
        (tree / "Album").chmod(0o000)
        try:
            await rescan(client, workspace, identity_database, identity_settings)
        finally:
            (tree / "Album").chmod(0o755)

        assert (await summary(client, year.id))["path"] == "Album/2026", (
            "a blocked subtree yields no conclusions about what is inside it"
        )
        assert (await latest_run(client, workspace))["folders_transferred"] == 0


# ------------------------------------------------------------------------- and the numbers


@pytest.mark.fr("F-015/FR-7", "F-015/FR-8")
async def test_a_transfer_leaves_every_folder_total_right(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Where this could go quietly wrong.

    The transfer deletes a folder row, and the rollup queue cascades on that — so a change queued
    against the discarded row would vanish with it, leaving a number permanently short. Settling
    the queue first is what makes the totals it inherits the truth.
    """
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        assert album is not None
        before = (await summary(client, album.id))["aggregates"]
        assert (before["total_files"], before["pending"]) == (3, False)

        (tree / "Album").rename(tree / "Photos")
        await rescan(client, workspace, identity_database, identity_settings)
        await rollup_pending(identity_database, identity_settings)

        after = (await summary(client, album.id))["aggregates"]
        assert after["total_files"] == 3, "the same three files, under the same folder id"
        assert after["total_bytes"] == before["total_bytes"]
        root = await folder_at(identity_database, workspace, "")
        assert root is not None
        assert (await summary(client, root.id))["aggregates"]["total_files"] == 3

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-7", "F-015/FR-8")
async def test_a_transfer_with_changes_still_queued_ends_at_ground_truth(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The transfer does not wait for the totals to catch up, so it has to carry them.

    Nothing drains the queue here: the rescan moves a directory to a different parent while every
    change from the import is still queued. Three things then have to be true at once — the
    discarded row's queued changes were handed over rather than deleted with it, the survivor's own
    were compensated onto the chain they were written for, and its totals were added to rather than
    overwritten. Any one of them wrong shows up as a folder that disagrees with its own files.
    """
    tree = tmp_path / "nas"
    build(tree, {"Album/beach.jpg": b"a photo", "Album/2026/lake.jpg": b"another", "Box/.k": b""})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        assert album is not None
        assert (await summary(client, album.id))["aggregates"]["pending"] is True, (
            "the import's changes are queued and deliberately left that way"
        )

        (tree / "Album").rename(tree / "Box" / "Photos")
        await rescan(client, workspace, identity_database, identity_settings)

        after = await summary(client, album.id)
        assert after["id"] == str(album.id)
        assert after["path"] == "Box/Photos"

        await rollup_pending(identity_database, identity_settings)
        assert (await summary(client, album.id))["aggregates"]["total_files"] == 2
        box = await folder_at(identity_database, workspace, "Box")
        assert box is not None
        # Two from the moved subtree plus the file `Box` already held.
        assert (await summary(client, box.id))["aggregates"]["total_files"] == 3

    await every_folder_matches_ground_truth(identity_database)


@pytest.mark.fr("F-015/FR-7")
async def test_a_subtree_rescan_concludes_nothing_about_folders_outside_it(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A run that only read one subtree never looked for the directories outside it.

    So a folder outside cannot be *vanished* as far as this run is concerned, however much of its
    content turns up inside — the same bound the reconciliation sweep obeys, one level up.
    """
    tree = tmp_path / "nas"
    build(tree, {"Outside/a.txt": b"one", "Inside/keep.txt": b"kept"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        outside = await folder_at(identity_database, workspace, "Outside")
        assert outside is not None

        # The content moves in, and the directory it came from disappears — but only `Inside` is
        # rescanned, so nothing about `Outside` was looked at.
        (tree / "Inside" / "Moved").mkdir()
        (tree / "Outside" / "a.txt").rename(tree / "Inside" / "Moved" / "a.txt")
        (tree / "Outside").rmdir()
        response = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
            json={"path": "Inside"},
            headers=SAME_ORIGIN,
        )
        assert response.status_code == 202, response.text
        await scan_pending(identity_database, identity_settings)

        assert (await summary(client, outside.id))["path"] == "Outside", (
            "a subtree rescan may not hand away the identity of a folder it never looked for"
        )
        assert (await latest_run(client, workspace))["folders_transferred"] == 0

        # And it stays that way, which is the honest cost of the bound: the evidence belonged to
        # the run that saw the files move, so a later whole-workspace pass has none to read. The
        # content is registered where it now is, under a new folder identity.
        await rescan(client, workspace, identity_database, identity_settings)
        assert (await summary(client, outside.id))["path"] == "Outside"
        assert (await latest_run(client, workspace))["folders_transferred"] == 0


@pytest.mark.fr("F-015/FR-7")
async def test_files_and_subfolders_pointing_different_ways_is_ambiguous(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Two readings of the evidence, two answers: the folder was taken apart, not renamed.

    Its file went one way and its subdirectory went another, so neither destination is *the*
    folder — and picking the louder half would put a subtree grant somewhere arbitrary.
    """
    tree = tmp_path / "nas"
    build(tree, {"Album/note.txt": b"a note", "Album/2026/lake.jpg": b"a photo"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        year = await folder_at(identity_database, workspace, "Album/2026")
        assert album is not None and year is not None

        (tree / "Notes").mkdir()
        (tree / "Pictures").mkdir()
        (tree / "Album" / "note.txt").rename(tree / "Notes" / "note.txt")
        (tree / "Album" / "2026").rename(tree / "Pictures" / "2026")
        (tree / "Album").rmdir()
        await rescan(client, workspace, identity_database, identity_settings)

        # The subdirectory is unambiguous — its own files vouch for it — and it moved.
        assert (await summary(client, year.id))["path"] == "Pictures/2026"
        # Its parent is not: one file says `Notes`, one child folder says `Pictures`.
        assert (await summary(client, album.id))["path"] == "Album"
        run = await latest_run(client, workspace)
        assert (run["folders_transferred"], run["folders_ambiguous"]) == (1, 1)
        await rollup_pending(identity_database, identity_settings)

    await every_folder_matches_ground_truth(identity_database)
