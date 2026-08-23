"""Folders as objects: creating them, browsing them, and moving them without losing anything.

[F-015](../../features/F-015-folders.md)'s phase-1 surface. Two things are asserted everywhere,
because they are what make a folder an entity rather than a path string:

- **the id survives** every rename and move, and everything that hangs off it travels along;
- **the disk and the index agree** afterwards — a rename that updated rows and not the directory
  would leave every derived path pointing at nothing.

The refusals get as much attention as the successes. A folder that quietly merged into an
occupied name, or moved into itself, or was "moved" across filesystems by copying ten terabytes
inside a request, would each be a worse outcome than an error naming the reason.
"""

from __future__ import annotations

import errno
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import files, filestore, folders, names
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.tables import file as file_table
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.test_scanning import adopt, build, user_files
from tests.upload_helpers import create_upload
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    create_member,
    create_workspace,
    instance,
    provision_pending,
    scan_pending,
    signed_in,
    workspace_ready,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TREE: dict[str, bytes] = {
    "notes.txt": b"a plain file",
    "Album/beach.jpg": b"a photo",
    "Album/2026/party.jpg": b"another photo",
}


def folders_url(workspace_id: UUID) -> str:
    return f"{API_V1_PREFIX}/workspaces/{workspace_id}/folders"


def folder_url(folder_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/folders/{folder_id}"


async def make_folder(
    client: httpx.AsyncClient, workspace: UUID, name: str, *, parent: UUID | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"name": name}
    if parent is not None:
        body["parent"] = str(parent)
    return await client.post(folders_url(workspace), json=body, headers=SAME_ORIGIN)


async def move(
    client: httpx.AsyncClient,
    folder_id: UUID,
    *,
    parent: UUID | None = None,
    name: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {}
    if parent is not None:
        body["parent"] = str(parent)
    if name is not None:
        body["name"] = name
    return await client.post(f"{folder_url(folder_id)}/move", json=body, headers=SAME_ORIGIN)


async def children(client: httpx.AsyncClient, folder_id: UUID, **params: Any) -> dict[str, Any]:
    response = await client.get(f"{folder_url(folder_id)}/children", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def folder_at(database_url: str, workspace: UUID, path: str) -> folders.Folder:
    """The folder row at this workspace-relative path — the test's own resolver."""
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


async def file_ids(database_url: str) -> dict[str, UUID]:
    """Every live file by its derived path — what a move must leave unchanged."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            found: dict[str, UUID] = {}
            live = select(file_table.c.id).where(file_table.c.state == "live")
            rows = (await connection.execute(live)).scalars().all()
            for identifier in rows:
                record = await files.get(connection, identifier)
                assert record is not None
                found[await files.path_of(connection, record)] = record.id
            return found
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------- creating


@pytest.mark.fr("F-015/FR-1", "F-015/FR-11")
async def test_a_created_folder_exists_on_disk_and_in_the_index(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A folder whose directory does not exist would be a lie about the storage (ADR-0003)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await make_folder(client, workspace, "Documents")
        assert created.status_code == 201, created.text
        nested = await make_folder(client, workspace, "Tax", parent=UUID(created.json()["id"]))

    body = created.json()
    assert body["name"] == "Documents"
    assert body["path"] == "Documents"
    assert body["depth"] == 1
    assert (root / "Documents").is_dir()

    assert nested.status_code == 201, nested.text
    assert nested.json()["path"] == "Documents/Tax"
    assert (root / "Documents" / "Tax").is_dir()

    created_events = await read_events(identity_database, action="folder.created")
    # The root folder is created by provisioning, so this is the second and third.
    assert [event["details"]["name"] for event in created_events] == ["", "Documents", "Tax"]
    assert created_events[-1]["actor_type"] == "user"


@pytest.mark.fr("F-015/FR-6")
async def test_a_folder_cannot_take_a_name_a_sibling_holds(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """On the comparison key, so `documents` collides with `Documents` (ADR-0019)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        first = await make_folder(client, workspace, "Documents")
        assert first.status_code == 201, first.text

        same = await make_folder(client, workspace, "Documents")
        folded = await make_folder(client, workspace, "documents")
        decomposed = await make_folder(client, workspace, "Café")
        composed = await make_folder(client, workspace, "Café")

    assert same.status_code == 409
    assert folded.status_code == 409
    assert decomposed.status_code == 201, decomposed.text
    # The NFD spelling was stored as NFC, so the NFC request collides with it.
    assert decomposed.json()["name"] == "Café"
    assert composed.status_code == 409


async def test_a_folder_cannot_take_a_name_a_file_holds(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A directory entry is a file or a folder, never both."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        uploaded = await create_upload(client, workspace, "notes.txt", body=b"a file")
        assert uploaded.status_code == 201, uploaded.text

        refused = await make_folder(client, workspace, "notes.txt")

    assert refused.status_code == 409
    assert "A file named" in refused.json()["detail"], "the reason has to name what is in the way"


@pytest.mark.fr("F-015/FR-6")
@pytest.mark.parametrize("name", ["x" * 256, "tab\there", "a/b", ".", ".."])
async def test_a_name_the_policy_refuses_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path, name: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        response = await make_folder(client, workspace, name)

    assert response.status_code == 422, response.text


@pytest.mark.fr("F-001/FR-13")
@pytest.mark.parametrize("name", [names.CONTROL_DIRECTORY, ".WORKSPACE", ".Workspace"])
async def test_the_control_directory_cannot_be_created_at_a_workspace_root(
    identity_settings: Settings, identity_database: str, name: str
) -> None:
    """A11: `validate_name` carried the check and no API call site ever asked for it.

    The exact name adopted the app's own control directory as a user folder — and then the
    scanner, which skips that key at the root before recording what it saw, concluded the row
    was absent from a directory it had read successfully. The next scheduled scan trashed the
    folder and everything under it while the files sat intact on disk. Case variants matter for
    the same reason: the reservation is on the comparison key, which is what the scanner skips.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        response = await make_folder(client, workspace, name)

        assert response.status_code == 422, response.text
        assert "reserved" in response.text
        # And the app's own control directory is still the app's.
        assert (root / names.CONTROL_DIRECTORY / "marker").is_file()


@pytest.mark.fr("F-001/FR-13")
async def test_a_folder_cannot_be_renamed_onto_the_control_directorys_name(
    identity_settings: Settings, identity_database: str
) -> None:
    """The same reservation, reached through the other door — and only at the root.

    Below the root the name is ordinary, because nothing of the app's lives there, so the same
    rename one level down has to succeed: this is a rule about a place, not about a string.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        at_root = await make_folder(client, workspace, "Album")
        assert at_root.status_code == 201, at_root.text
        parent = UUID(at_root.json()["id"])
        nested = await make_folder(client, workspace, "Nested", parent=parent)
        assert nested.status_code == 201, nested.text

        refused = await move(client, UUID(at_root.json()["id"]), name=names.CONTROL_DIRECTORY)
        allowed = await move(client, UUID(nested.json()["id"]), name=names.CONTROL_DIRECTORY)

    assert refused.status_code == 422, refused.text
    assert "reserved" in refused.text
    assert allowed.status_code == 200, allowed.text


@pytest.mark.fr("F-001/FR-13")
async def test_a_folder_moved_to_the_root_cannot_take_the_control_directorys_name(
    identity_settings: Settings, identity_database: str
) -> None:
    """Moving *and* renaming in one request is one operation, so one check covers both."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        holder = await make_folder(client, workspace, "Album")
        assert holder.status_code == 201, holder.text
        nested = await make_folder(
            client, workspace, names.CONTROL_DIRECTORY, parent=UUID(holder.json()["id"])
        )
        assert nested.status_code == 201, "the name is ordinary below the root"

        root = await folder_at(identity_database, workspace, "")
        refused = await move(client, UUID(nested.json()["id"]), parent=root.id)

    assert refused.status_code == 422, refused.text
    assert "reserved" in refused.text


@pytest.mark.fr("F-001/FR-13")
async def test_a_file_cannot_take_the_control_directorys_name_at_the_root(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The third door: a *file* renamed or moved onto the reserved name at a workspace root.

    Both shapes, because a move with no new name still carries its old one somewhere new — and
    below the root the same name is ordinary, which is what the successful half asserts.
    """
    tree = tmp_path / "nas"
    build(tree, {"Album/notes.txt": b"a plain file"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        identifiers = await file_ids(identity_database)
        root = await folder_at(identity_database, workspace, "")

        renamed = await client.post(
            f"{API_V1_PREFIX}/files/{identifiers['Album/notes.txt']}/move",
            json={"folder": str(root.id), "name": names.CONTROL_DIRECTORY},
            headers=SAME_ORIGIN,
        )
        # Allowed where nothing of the app's lives, then refused for the move alone.
        allowed = await client.post(
            f"{API_V1_PREFIX}/files/{identifiers['Album/notes.txt']}/move",
            json={"name": names.CONTROL_DIRECTORY},
            headers=SAME_ORIGIN,
        )
        moved = await client.post(
            f"{API_V1_PREFIX}/files/{identifiers['Album/notes.txt']}/move",
            json={"folder": str(root.id)},
            headers=SAME_ORIGIN,
        )

    assert renamed.status_code == 422, renamed.text
    assert allowed.status_code == 200, allowed.text
    assert moved.status_code == 422, moved.text
    assert "reserved" in moved.text
    # The app's own control directory is untouched, and the file is still where it was.
    assert (tree / names.CONTROL_DIRECTORY / "marker").is_file()
    assert (tree / "Album" / names.CONTROL_DIRECTORY).is_file()


async def test_creating_a_folder_whose_directory_exists_adopts_it_untouched(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Creating a folder is idempotent on disk, and adopting is not modifying.

    Someone copied a directory onto the storage and no scan has run yet. Refusing would be
    unhelpful and inconsistent — an upload's parent directories are created the same way — while
    *touching* what is inside it would break ADR-0019's rule. So the row is created, the contents
    are left exactly as they are, and the next scan registers them.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        (root / "Already").mkdir()
        (root / "Already" / "there.txt").write_bytes(b"put here by hand")

        created = await make_folder(client, workspace, "Already")
        assert created.status_code == 201, created.text
        listed = await children(client, UUID(created.json()["id"]))

    assert (root / "Already" / "there.txt").read_bytes() == b"put here by hand"
    assert listed["data"] == [], "the contents are the next scan's business, not this call's"


# -------------------------------------------------------------------------- browsing


@pytest.mark.fr("F-015/FR-5")
async def test_children_are_folders_first_then_files(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """One ordered stream: directories by name, then files by the requested key."""
    tree = tmp_path / "nas"
    build(tree, {"b.txt": b"bb", "a.txt": b"a", "Zoo/x.txt": b"x", "Album/y.txt": b"y"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        page = await children(client, root.id)

    assert [(item["kind"], item["name"]) for item in page["data"]] == [
        ("folder", "Album"),
        ("folder", "Zoo"),
        ("file", "a.txt"),
        ("file", "b.txt"),
    ]
    assert page["next_cursor"] is None
    assert [item["path"] for item in page["data"]] == ["Album", "Zoo", "a.txt", "b.txt"]
    assert [item["size"] for item in page["data"] if item["kind"] == "file"] == [1, 2]


@pytest.mark.fr("F-015/FR-5")
async def test_a_page_is_filled_across_the_seam(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A folder with two subfolders and many files must not return a page of two."""
    tree = tmp_path / "nas"
    contents = {f"f{index:02d}.txt": b"x" for index in range(6)}
    contents.update({"A/x.txt": b"x", "B/y.txt": b"y"})
    build(tree, contents)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        page = await children(client, root.id, limit=4)

    assert [item["name"] for item in page["data"]] == ["A", "B", "f00.txt", "f01.txt"]
    assert page["next_cursor"] is not None


@pytest.mark.fr("F-015/FR-5")
async def test_every_child_appears_exactly_once_across_pages(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-9 at a size a test can afford: the guarantee is the keyset, not the row count.

    Walked at a page size that does not divide the total, so a seam falls inside both segments.
    """
    tree = tmp_path / "nas"
    contents: dict[str, bytes] = {f"file-{index:03d}.txt": b"x" for index in range(40)}
    contents.update({f"dir-{index:02d}/x.txt": b"x" for index in range(9)})
    build(tree, contents)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(20):
            page = await children(
                client, root.id, limit=7, **({"cursor": cursor} if cursor else {})
            )
            seen.extend(f"{item['kind']}:{item['name']}" for item in page["data"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

    assert cursor is None, "the listing did not finish"
    assert len(seen) == len(set(seen)) == 49
    assert seen == sorted(seen, key=lambda entry: (entry.split(":")[0] != "folder", entry))


@pytest.mark.fr("F-015/FR-5")
async def test_files_can_be_ordered_by_size_and_by_timestamp(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree, {"small.txt": b"x", "large.txt": b"x" * 100, "middle.txt": b"x" * 10})
    old = datetime.now(tz=UTC) - timedelta(days=2)
    os.utime(tree / "large.txt", (old.timestamp(), old.timestamp()))

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        by_size = await children(client, root.id, sort="size")
        by_time = await children(client, root.id, sort="modified")

    assert [item["name"] for item in by_size["data"]] == ["small.txt", "middle.txt", "large.txt"]
    assert by_time["data"][0]["name"] == "large.txt"


async def test_changing_the_ordering_mid_pagination_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A cursor carries the ordering it was made under: reusing it under another one would skip
    or repeat rows at the seam, and the client would have no way to notice."""
    tree = tmp_path / "nas"
    build(tree, {f"f{index}.txt": b"x" * index for index in range(1, 6)})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        first = await children(client, root.id, limit=2)
        response = await client.get(
            f"{folder_url(root.id)}/children",
            params={"limit": 2, "sort": "size", "cursor": first["next_cursor"]},
        )
        nonsense = await client.get(
            f"{folder_url(root.id)}/children", params={"cursor": "not-a-cursor"}
        )

    assert response.status_code == 422
    assert nonsense.status_code == 422


# -------------------------------------------------------------------- renaming, moving


@pytest.mark.fr("F-015/FR-1", "F-015/FR-3", "F-015/FR-4", "F-015/FR-11")
async def test_renaming_a_folder_keeps_its_identity_and_renames_the_directory(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-1: the id survives, the contents report new paths, the directory moved."""
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        before = await file_ids(identity_database)

        renamed = await move(client, album.id, name="Album 2026")
        assert renamed.status_code == 200, renamed.text

    assert renamed.json()["id"] == str(album.id), "a rename created a new folder"
    assert renamed.json()["path"] == "Album 2026"
    assert (tree / "Album 2026" / "beach.jpg").is_file()
    assert not (tree / "Album").exists()

    after = await file_ids(identity_database)
    assert after == {
        "notes.txt": before["notes.txt"],
        "Album 2026/beach.jpg": before["Album/beach.jpg"],
        "Album 2026/2026/party.jpg": before["Album/2026/party.jpg"],
    }
    events = await read_events(identity_database, action="folder.renamed")
    assert [(event["details"]["from"], event["details"]["to"]) for event in events] == [
        ("Album", "Album 2026")
    ]


@pytest.mark.fr("F-015/FR-2", "F-015/FR-3", "F-015/FR-4", "F-015/FR-11")
async def test_moving_a_folder_moves_its_whole_subtree(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """One rename on disk, one closure rewrite — and every derived path follows."""
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        inner = await folder_at(identity_database, workspace, "Album/2026")
        before = await file_ids(identity_database)

        holder = await make_folder(client, workspace, "Pictures")
        assert holder.status_code == 201, holder.text
        moved = await move(client, album.id, parent=UUID(holder.json()["id"]))
        assert moved.status_code == 200, moved.text
        deep = await client.get(folder_url(inner.id))

    assert moved.json()["path"] == "Pictures/Album"
    assert moved.json()["depth"] == 2
    # The descendant's own path and depth came from the closure, not from the request.
    assert deep.json()["path"] == "Pictures/Album/2026"
    assert deep.json()["depth"] == 3
    assert (tree / "Pictures" / "Album" / "2026" / "party.jpg").is_file()

    after = await file_ids(identity_database)
    assert after["Pictures/Album/2026/party.jpg"] == before["Album/2026/party.jpg"]
    assert [
        event["details"]["to"]
        for event in await read_events(identity_database, action="folder.moved")
    ] == ["Pictures/Album"]


@pytest.mark.fr("F-015/FR-4")
async def test_a_folder_cannot_move_into_itself(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-3: the tree would stop being one, so the cycle is named rather than attempted."""
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        inner = await folder_at(identity_database, workspace, "Album/2026")

        into_itself = await move(client, album.id, parent=album.id)
        into_descendant = await move(client, album.id, parent=inner.id)

    assert into_itself.status_code == 409
    assert into_descendant.status_code == 409
    assert "inside itself" in into_descendant.json()["detail"]
    assert (tree / "Album" / "2026").is_dir(), "a refused move touched the storage"


@pytest.mark.fr("F-015/FR-4")
async def test_a_move_onto_an_occupied_name_is_refused_rather_than_merged(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-3's other half: no merge in v1, because merging is not undoable."""
    tree = tmp_path / "nas"
    build(tree, {"Album/x.txt": b"x", "Pictures/Album/y.txt": b"y"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        album = await folder_at(identity_database, workspace, "Album")
        pictures = await folder_at(identity_database, workspace, "Pictures")

        refused = await move(client, album.id, parent=pictures.id)

    assert refused.status_code == 409
    assert (tree / "Album" / "x.txt").is_file()
    assert (tree / "Pictures" / "Album" / "y.txt").is_file()


@pytest.mark.fr("F-015/FR-1")
async def test_the_workspace_root_cannot_be_renamed_or_moved(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """It *is* the workspace's directory, so renaming it is a workspace operation."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        root = await folder_at(identity_database, workspace, "")
        refused = await move(client, root.id, name="Renamed")

    assert refused.status_code == 409
    assert "workspace" in refused.json()["detail"]


@pytest.mark.fr("F-015/FR-4")
async def test_a_cross_workspace_move_keeps_every_identity(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-4 for the case one rename can do: both roots on one filesystem.

    The subtree changes workspace in the same transaction as the files inside it — either half
    alone would break the containment invariant, which is why its constraint is deferrable.
    """
    async with workspace_ready(identity_settings, identity_database, name="Source") as (
        client,
        source,
        source_root,
    ):
        second = await create_workspace(client, "Target")
        assert second.status_code == 201, second.text
        target = UUID(second.json()["id"])
        await provision_pending(identity_database)

        made = await make_folder(client, source, "Album")
        assert made.status_code == 201, made.text
        uploaded = await create_upload(client, source, "Album/beach.jpg", body=b"a photo")
        assert uploaded.status_code == 201, uploaded.text
        file_id = UUID(uploaded.json()["id"])

        target_root = await folder_at(identity_database, target, "")
        moved = await move(client, UUID(made.json()["id"]), parent=target_root.id)
        assert moved.status_code == 200, moved.text
        summary = await client.get(f"{API_V1_PREFIX}/files/{file_id}")
        content = await client.get(f"{API_V1_PREFIX}/files/{file_id}/content")

    assert moved.json()["workspace"] == str(target)
    assert moved.json()["path"] == "Album"
    assert summary.json()["id"] == str(file_id), "the file lost its identity"
    assert summary.json()["workspace"] == str(target)
    assert summary.json()["path"] == "Album/beach.jpg"
    assert content.status_code == 200 and content.content == b"a photo"
    assert not (source_root / "Album").exists()


@pytest.mark.fr("F-015/FR-4")
async def test_a_move_across_filesystems_is_refused_with_the_reason(
    identity_settings: Settings, identity_database: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """Terabytes are not moved inside a request, and pretending otherwise is worse than a `409`.

    Two filesystems cannot be mounted in a test without privileges, so the kernel's own answer is
    injected instead: `rename` across filesystems fails with `EXDEV` and changes nothing, which is
    exactly the condition the code reads.
    """

    def across_filesystems(source: Path, destination: Path) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(filestore, "move_entry", across_filesystems)

    async with workspace_ready(identity_settings, identity_database, name="Source") as (
        client,
        source,
        source_root,
    ):
        second = await create_workspace(client, "Target")
        target = UUID(second.json()["id"])
        await provision_pending(identity_database)
        made = await make_folder(client, source, "Album")
        target_root = await folder_at(identity_database, target, "")

        refused = await move(client, UUID(made.json()["id"]), parent=target_root.id)

    assert refused.status_code == 409
    assert "different filesystems" in refused.json()["detail"]
    assert (source_root / "Album").is_dir(), "a refused move moved the directory anyway"


# ----------------------------------------------------------------------- moving files


@pytest.mark.fr("F-010/FR-1")
async def test_a_file_can_be_renamed_and_moved_keeping_its_identity(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The operation the deferred auto-sorter is built on: identity and history travel."""
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        before = await file_ids(identity_database)
        album = await folder_at(identity_database, workspace, "Album")

        renamed = await client.post(
            f"{API_V1_PREFIX}/files/{before['notes.txt']}/move",
            json={"name": "reminders.txt"},
            headers=SAME_ORIGIN,
        )
        assert renamed.status_code == 200, renamed.text
        moved = await client.post(
            f"{API_V1_PREFIX}/files/{before['notes.txt']}/move",
            json={"folder": str(album.id)},
            headers=SAME_ORIGIN,
        )
        assert moved.status_code == 200, moved.text
        content = await client.get(f"{API_V1_PREFIX}/files/{before['notes.txt']}/content")

    assert renamed.json()["path"] == "reminders.txt"
    assert moved.json()["path"] == "Album/reminders.txt"
    assert moved.json()["id"] == str(before["notes.txt"])
    assert content.status_code == 200 and content.content == b"a plain file"
    assert (tree / "Album" / "reminders.txt").is_file()
    assert not (tree / "notes.txt").exists()

    events = await read_events(identity_database, action="file.moved")
    assert [event["details"]["detected"] for event in events] == ["api", "api"]
    assert [event["actor_type"] for event in events] == ["user", "user"]


async def test_a_file_cannot_be_moved_onto_an_occupied_name(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    tree = tmp_path / "nas"
    build(tree, {"a.txt": b"a", "Album/a.txt": b"other a"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        identifiers = await file_ids(identity_database)
        album = await folder_at(identity_database, workspace, "Album")

        refused = await client.post(
            f"{API_V1_PREFIX}/files/{identifiers['a.txt']}/move",
            json={"folder": str(album.id)},
            headers=SAME_ORIGIN,
        )

    assert refused.status_code == 409
    assert (tree / "a.txt").read_bytes() == b"a"
    assert (tree / "Album" / "a.txt").read_bytes() == b"other a"


# ------------------------------------------------------------- what must never happen


@pytest.mark.fr("F-015/FR-12")
async def test_another_users_folder_does_not_exist(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The same `404` for absent and for someone else's — existence is never leaked (08)."""
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            created = await create_workspace(admin, "Private")
            workspace = UUID(created.json()["id"])
            await provision_pending(identity_database)
            await create_member(admin)
            folder = await make_folder(admin, workspace, "Clients")
            assert folder.status_code == 201, folder.text
            folder_id = UUID(folder.json()["id"])

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            read = await member.get(folder_url(folder_id))
            listed = await member.get(f"{folder_url(folder_id)}/children")
            moved = await move(member, folder_id, name="Theirs")
            created_there = await make_folder(member, workspace, "Mine")

    assert [read.status_code, listed.status_code, moved.status_code] == [404, 404, 404]
    assert created_there.status_code == 404


@pytest.mark.fr("F-015/FR-4")
async def test_a_crash_between_the_rename_and_the_rows_converges_on_the_next_scan(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Why the disk is written first, asserted from the state such a crash leaves behind.

    The directory has moved and the index still points at the old path. The next scan finds the
    old path gone and the new one full, and 6b's content matching moves every file back onto its
    own identity — so nothing is lost and nothing is duplicated. (The *folder* row is still a new
    one until F-015/FR-7 transfers folder identity too.)
    """
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        before = await file_ids(identity_database)

        # Exactly what a crash after `filestore.move_entry` and before the row update leaves.
        (tree / "Album").rename(tree / "Album 2026")
        rescan = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        assert rescan.status_code == 202, rescan.text
        await scan_pending(identity_database, identity_settings)
        report = await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}/import-status")

    after = await file_ids(identity_database)
    assert set(after) == {"notes.txt", "Album 2026/beach.jpg", "Album 2026/2026/party.jpg"}
    assert after["Album 2026/beach.jpg"] == before["Album/beach.jpg"], "identity was lost"
    assert after["Album 2026/2026/party.jpg"] == before["Album/2026/party.jpg"]
    latest = report.json()["recent"][0]
    assert latest["files_moved"] == 2
    assert latest["files_trashed"] == 0
    assert latest["files_registered"] == 0


@pytest.mark.fr("F-001/FR-13")
async def test_the_control_directory_is_in_no_listing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The half of FR-13 that needed a listing endpoint to be testable at all."""
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        root = await folder_at(identity_database, workspace, "")
        page = await children(client, root.id, limit=100)

    assert names.CONTROL_DIRECTORY not in str(page)
    assert (tree / names.CONTROL_DIRECTORY).is_dir(), "it is there — it is just never listed"


@pytest.mark.fr("F-014/FR-1")
async def test_a_trashed_file_is_in_no_listing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A file someone deleted on the storage must not appear in the folder they are browsing."""
    tree = tmp_path / "nas"
    build(tree, {"kept.txt": b"kept", "gone.txt": b"gone"})

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        (tree / "gone.txt").unlink()
        response = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        assert response.status_code == 202, response.text
        await scan_pending(identity_database, identity_settings)

        root = await folder_at(identity_database, workspace, "")
        page = await children(client, root.id)

    assert [item["name"] for item in page["data"]] == ["kept.txt"]


async def test_nothing_a_move_refuses_touches_the_tree(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Every refusal above, in one before-and-after fingerprint of the whole tree."""
    tree = tmp_path / "nas"
    build(tree, TREE)

    async with adopt(identity_settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, identity_settings)
        expected = user_files(tree)
        album = await folder_at(identity_database, workspace, "Album")
        inner = await folder_at(identity_database, workspace, "Album/2026")
        root = await folder_at(identity_database, workspace, "")

        assert (await move(client, album.id, parent=inner.id)).status_code == 409
        assert (await move(client, root.id, name="Nope")).status_code == 409
        assert (await move(client, album.id, name="x" * 256)).status_code == 422
        assert (await move(client, uuid4(), name="Nope")).status_code == 404

    assert user_files(tree) == expected


@pytest.mark.fr("F-015/FR-12", "F-015/FR-13")
async def test_a_path_is_rendered_from_the_root_the_caller_may_see(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The mechanism grants will use, asserted before grants exist.

    Phase 1 has one permission, so every path an owner sees starts at the workspace root. What
    has to be true *now* is that a path is computed from a root at all — and that asking for one
    relative to a folder that does not contain it fails rather than quietly returning the full
    path, because in phase 4 that fallback would be the leak (F-015/FR-13).
    """
    tree = tmp_path / "nas"
    build(tree, {"Private/Clients/Acme/deal.pdf": b"a contract"})

    async with adopt(identity_settings, identity_database, tree) as (_client, workspace):
        await scan_pending(identity_database, identity_settings)

    acme = await folder_at(identity_database, workspace, "Private/Clients/Acme")
    clients = await folder_at(identity_database, workspace, "Private/Clients")
    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            root = await folders.root_of(connection, workspace)
            assert root is not None

            # An owner's root is the workspace root, so they see the whole path...
            assert await folders.path_of(connection, acme, relative_to=root.id) == (
                "Private/Clients/Acme"
            )
            # ...and the same folder rendered from a deeper root carries nothing above it, which
            # is what a grantee will receive.
            assert await folders.path_of(connection, acme, relative_to=clients.id) == "Acme"
            assert await folders.path_of(connection, acme, relative_to=acme.id) == ""

            with pytest.raises(folders.NotAnAncestorError):
                await folders.path_of(connection, clients, relative_to=acme.id)
    finally:
        await engine.dispose()
