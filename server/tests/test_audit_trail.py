"""The audit trail's phase-1 obligations ([F-011](../../features/F-011-audit-trail.md)).

The log is built in phase 1 because "every mutation is logged in its own transaction" has to hold
from the *first* mutation ([ROADMAP](../../ROADMAP.md) phase 1); the query API and the user-facing
view arrive in phase 4 with FR-5 and FR-6. What is testable now is the shape of the record and the
guarantees around writing it: one row per action, no fewer, carrying enough of the resource to
still mean something after the resource itself is gone.

`test_events.py` covers the write protocol from below — same connection, same transaction. This
module covers what a *reader* of the trail is promised.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.tables import file, folder, workspace
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.workspace_helpers import as_admin, create_workspace, provision_pending, scan_pending

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Enough files that "one event each" is a claim rather than a coincidence, spread over several
#: directories so the count cannot have come from a single listing.
BULK: dict[str, bytes] = {
    "one.txt": b"1",
    "two.txt": b"2",
    "three.txt": b"3",
    "a/four.txt": b"4",
    "a/five.txt": b"5",
    "a/six.txt": b"6",
    "a/b/seven.txt": b"7",
    "a/b/eight.txt": b"8",
    "a/b/c/nine.txt": b"9",
    "a/b/c/ten.txt": b"10",
    "a/b/c/eleven.txt": b"11",
    "a/b/c/twelve.txt": b"12",
}


def build(root: Path, tree: dict[str, bytes]) -> None:
    for relative, content in tree.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


@asynccontextmanager
async def adopted(
    settings: Settings, database_url: str, tree: Path
) -> AsyncGenerator[tuple[httpx.AsyncClient, UUID]]:
    """Adopt a directory, provision it, run its initial scan — and keep the session open.

    Yields rather than returns because the assertions that follow are part of the same session:
    the workspace and the credential that made it are one setup, not two.
    """
    async with as_admin(settings, adoption_roots=(tree,)) as client:
        created = await create_workspace(client, "The NAS", adopt_path=tree)
        assert created.status_code == 201, created.text
        await provision_pending(database_url)
        await scan_pending(database_url, settings)
        yield client, UUID(created.json()["id"])


async def only_file(database_url: str) -> UUID:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(select(file.c.id))).scalars().one()
    finally:
        await engine.dispose()


@pytest.mark.fr("F-011/FR-3")
async def test_a_bulk_registration_records_every_single_item(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Twelve files, twelve events — no coalescing, no summary row standing in for the set.

    The temptation is real: a scan that registered ten thousand files could write one
    `workspace.scanned` event and be done. That is what FR-3 forbids, because the question the
    trail exists to answer is "where did *this* file come from", one file at a time. Coalescing
    belongs in the live-update layer ([F-012](../../features/F-012-live-updates.md)), which has a
    different audience and no memory.
    """
    tree = tmp_path / "nas"
    build(tree, BULK)

    async with adopted(identity_settings, identity_database, tree):
        pass

    created = await read_events(identity_database, action="file.created")

    assert len(created) == len(BULK)
    assert sorted(str(row["details"]["path"]) for row in created) == sorted(BULK)
    # And the run-level event sits beside them, not instead of them.
    assert len(await read_events(identity_database, action="workspace.scanned")) == 1


@pytest.mark.fr("F-011/FR-1")
async def test_every_record_says_who_did_what_to_which_thing_and_when(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The five fields FR-1 names, on every row a whole instance's worth of setup produced."""
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"1", "a/two.txt": b"2"})

    async with adopted(identity_settings, identity_database, tree):
        pass

    rows = await read_events(identity_database)

    assert len(rows) > 5, "bootstrap, login, workspace, provisioning and a scan, at least"
    for row in rows:
        assert row["actor_type"] in {"user", "system", "extractor"}
        assert "." in str(row["action"]), "action types are namespaced (events.py)"
        assert row["resource_type"]
        assert row["occurred_at"] is not None
        assert isinstance(row["details"], dict)


@pytest.mark.fr("F-011/FR-9")
async def test_a_record_still_names_its_resource_after_the_resource_has_moved(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """`details` is a copy, not a join.

    A trail that resolved the file's path at *read* time would answer "where was this imported
    from?" with wherever the file happens to be now — and would answer nothing at all once the row
    is purged, which is the case FR-9 exists for
    ([F-014/FR-7](../../features/F-014-deletion-and-trash.md)).
    """
    tree = tmp_path / "nas"
    build(tree, {"invoices/original.txt": b"content"})

    async with adopted(identity_settings, identity_database, tree) as (client, _):
        target = await only_file(identity_database)
        moved = await client.post(
            f"{API_V1_PREFIX}/files/{target}/move",
            json={"name": "renamed.txt"},
            headers=SAME_ORIGIN,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["path"] == "invoices/renamed.txt"

    # The registration still describes the file as it was registered.
    created = await read_events(identity_database, action="file.created")
    assert str(created[0]["details"]["path"]) == "invoices/original.txt"

    # And the move records both ends, so the two rows read as a history rather than a snapshot.
    move = (await read_events(identity_database, action="file.moved"))[0]
    assert str(move["details"]["from"]) == "invoices/original.txt"
    assert str(move["details"]["to"]) == "invoices/renamed.txt"


@pytest.mark.fr("02/INV-6")
async def test_a_refused_request_leaves_neither_the_change_nor_its_event(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The invariant from outside: a rejected mutation is invisible in both tables.

    `test_events.py` proves the transaction boundary directly. This proves the API actually
    *uses* it — a handler that committed the row and then recorded the event would pass the unit
    test and fail here.
    """
    tree = tmp_path / "nas"
    build(tree, {"invoices/one.txt": b"1"})

    async with adopted(identity_settings, identity_database, tree) as (client, _):
        before = len(await read_events(identity_database))
        target = await only_file(identity_database)
        refused = await client.post(
            f"{API_V1_PREFIX}/files/{target}/move",
            json={"name": "not/a/name.txt"},
            headers=SAME_ORIGIN,
        )
        assert refused.status_code == 422, refused.text

    assert len(await read_events(identity_database)) == before


@pytest.mark.fr("02/INV-1")
async def test_nothing_can_be_orphaned_from_the_thing_that_owns_it(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Every file in exactly one folder, every folder in one workspace, every workspace to a user.

    Enforced by the schema rather than by the code that writes it, so the check attempts the
    violation from below — the level a future bug would arrive from.
    """
    tree = tmp_path / "nas"
    build(tree, {"one.txt": b"1"})

    async with adopted(identity_settings, identity_database, tree) as (_client, workspace_id):
        pass

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            for table, column in (
                (file, "folder_id"),
                (folder, "workspace_id"),
                (workspace, "owner_id"),
            ):
                with pytest.raises((IntegrityError, DBAPIError)):
                    await connection.execute(update(table).values(**{column: None}))
                await connection.rollback()

            # And the reference cannot be pointed at something that does not exist.
            with pytest.raises((IntegrityError, DBAPIError)):
                await connection.execute(
                    update(folder)
                    .where(folder.c.workspace_id == workspace_id)
                    .values(workspace_id=UUID("01a02900-0000-7000-8000-0000000000ff"))
                )
            await connection.rollback()
    finally:
        await engine.dispose()
