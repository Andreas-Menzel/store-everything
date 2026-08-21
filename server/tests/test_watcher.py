"""The doorbell: does it ring for the right directory, and is it harmless when it cannot?

The watcher is the one mechanism in this system that is *allowed* to fail. ADR-0019 makes it a
lossy accelerator in front of the scheduled scan, so the tests come in two halves:

- the **arithmetic** — which directory an event means, and when a burst of them becomes one scan —
  asserted from injected timestamps rather than sleeps, because that is where the logic is;
- the **failure paths** — a kernel that refuses the watch, a workspace that disappears, the whole
  thing switched off — each of which must leave the schedule untouched and say what happened.

Only one test uses a real observer on a real directory. It polls with a deadline rather than
sleeping a fixed time, so it is slow to fail rather than flaky, and it is the only place that can
prove the wiring from an inotify event through to a queued scan actually exists.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from watchdog.observers.api import BaseObserver

from store_everything import names, operations, scans, watcher, workspaces
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.tables import file, operation
from store_everything.tables import workspace as workspace_table
from tests.test_scanning import adopt, build
from tests.workspace_helpers import scan_pending

#: The pure rules below need no loop and no database; the integration tests each carry their own
#: marks, so running `-m "not integration"` still exercises the arithmetic.

#: Fast enough that a test does not wait on the product's defaults, slow enough that a burst is
#: still a burst.
QUIET = 0.1

#: Every timing assertion is a poll with a ceiling, never a sleep of a fixed length.
DEADLINE = 20.0


# ------------------------------------------------------------------- what an event means


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("notes.txt", ""),
        ("Photos/beach.jpg", "Photos"),
        ("Photos/2026/summer/IMG_1.jpg", "Photos/2026/summer"),
    ],
)
def test_a_file_event_means_the_directory_that_holds_it(changed: str, expected: str) -> None:
    """A scan works in directories, so that is what an event has to name.

    A new file, a modified file and a deleted file all mean the same thing — look at this
    directory again — so one rule covers all three.
    """
    assert watcher.subtree_for(Path("/nas"), Path("/nas") / changed, is_directory=False) == expected


@pytest.mark.parametrize(
    ("changed", "expected"),
    [("Photos", "Photos"), ("Photos/2026", "Photos/2026"), (".", "")],
)
def test_a_directory_event_means_that_directory(changed: str, expected: str) -> None:
    """ "Something in here changed" is what such an event says, so the directory itself is the
    target — using its parent instead would coarsen every event to the workspace root, since a
    write to a file also stirs the directory holding it."""
    assert (
        watcher.subtree_for(Path("/nas"), (Path("/nas") / changed).resolve(), is_directory=True)
        == expected
    )


def test_the_control_directory_is_not_a_reason_to_scan() -> None:
    """Not cosmetic: every upload chunk lands in `.workspace/staging/`, so without this the
    watcher would fire continuously on the app's own writes for as long as an upload runs."""
    root = Path("/nas")
    staging = root / names.CONTROL_DIRECTORY / "staging" / "part.partial"

    assert watcher.subtree_for(root, staging, is_directory=False) is None
    assert watcher.subtree_for(root, root / names.CONTROL_DIRECTORY, is_directory=True) is None


def test_an_event_outside_the_root_is_ignored() -> None:
    outside = Path("/somewhere/else/file.txt")
    assert watcher.subtree_for(Path("/nas"), outside, is_directory=False) is None


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("Photos/2026", "Photos/2025", "Photos"),
        ("Photos/2026/summer", "Photos/2026", "Photos/2026"),
        ("Photos", "Documents", ""),
        ("Photos/2026", "Photos/2026", "Photos/2026"),
        ("", "Photos/2026", ""),
    ],
)
def test_a_burst_collapses_to_the_directory_that_covers_it(
    first: str, second: str, expected: str
) -> None:
    """A copy touching two folders is one scan of the folder above them, not two scans."""
    assert watcher.common_ancestor(first, second) == expected


# ------------------------------------------------------------------------ when it rings


class Clock:
    """A hand-cranked monotonic clock, so burst timing is asserted rather than waited for."""

    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


def test_a_burst_that_is_still_noisy_is_not_due() -> None:
    clock = Clock()
    bursts = watcher.Bursts(now=clock)
    workspace = uuid4()

    bursts.note(workspace, "Photos")
    clock.advance(3.0)
    bursts.note(workspace, "Photos")

    assert bursts.take_due(quiet=5.0, max_hold=60.0) == []
    assert bursts.pending() == 1


def test_a_burst_that_has_gone_quiet_becomes_one_scan() -> None:
    clock = Clock()
    bursts = watcher.Bursts(now=clock)
    workspace = uuid4()

    bursts.note(workspace, "Photos/2026")
    bursts.note(workspace, "Photos/2025")
    clock.advance(5.0)

    assert bursts.take_due(quiet=5.0, max_hold=60.0) == [(workspace, "Photos")]
    # Taken, not copied: the next tick must not scan it again.
    assert bursts.pending() == 0


def test_a_burst_that_never_goes_quiet_is_still_acted_on() -> None:
    """An `rsync` writing for an hour would otherwise be noticed only when it finished.

    The maximum hold is what makes an import visible while it runs, and it is a consequence of
    the debounce window rather than a separate knob.
    """
    clock = Clock()
    bursts = watcher.Bursts(now=clock)
    workspace = uuid4()

    for _ in range(30):
        bursts.note(workspace, "Photos")
        clock.advance(2.0)

    assert bursts.take_due(quiet=5.0, max_hold=60.0) == [(workspace, "Photos")]


def test_one_workspace_going_quiet_does_not_take_another_with_it() -> None:
    clock = Clock()
    bursts = watcher.Bursts(now=clock)
    quiet_one, busy_one = uuid4(), uuid4()

    bursts.note(quiet_one, "Photos")
    clock.advance(5.0)
    bursts.note(busy_one, "Documents")

    assert bursts.take_due(quiet=5.0, max_hold=60.0) == [(quiet_one, "Photos")]
    assert bursts.pending() == 1


def test_forgetting_a_workspace_drops_its_pending_burst() -> None:
    """A workspace that is gone must not leave work behind that names it."""
    clock = Clock()
    bursts = watcher.Bursts(now=clock)
    workspace = uuid4()
    bursts.note(workspace, "Photos")

    bursts.forget(workspace)
    clock.advance(60.0)

    assert bursts.take_due(quiet=5.0, max_hold=60.0) == []


# ----------------------------------------------------------------- against a real tree


@pytest_asyncio.fixture
async def engine(identity_database: str) -> AsyncGenerator[AsyncEngine]:
    made = create_async_engine(identity_database)
    try:
        yield made
    finally:
        await made.dispose()


def watching_settings(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(
        update={"watcher_enabled": True, "watcher_debounce_seconds": QUIET, **overrides}
    )


@asynccontextmanager
async def running(
    engine: AsyncEngine, settings: Settings, **kwargs: Any
) -> AsyncGenerator[watcher.Watcher]:
    """Run a watcher for the duration of a `with` block, and stop it cleanly afterwards."""
    watching = watcher.Watcher(engine, settings, resubscribe_seconds=0.2, **kwargs)
    task = asyncio.create_task(watching.run_forever())
    try:
        yield watching
    finally:
        watching.stop()
        await asyncio.wait_for(task, timeout=DEADLINE)


async def until(condition: Callable[[], Any], *, what: str) -> Any:
    """Poll until the condition is truthy, or fail saying what never happened.

    Awaits whatever the condition returns if it needs awaiting, because the alternative — a
    coroutine object treated as a result — is truthy on the first try and would make every one of
    these assertions pass without waiting for anything.
    """
    deadline = asyncio.get_running_loop().time() + DEADLINE
    while asyncio.get_running_loop().time() < deadline:
        answer = condition()
        if inspect.isawaitable(answer):
            answer = await answer
        if answer:
            return answer
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} did not happen within {DEADLINE:.0f}s")


async def state_is(
    engine: AsyncEngine, workspace_id: UUID, expected: str
) -> tuple[str, str | None] | None:
    """The workspace's watch state, but only once it is the one being waited for."""
    state = await watch_state(engine, workspace_id)
    return state if state[0] == expected else None


async def watch_state(engine: AsyncEngine, workspace_id: UUID) -> tuple[str, str | None]:
    async with engine.connect() as connection:
        found = await workspaces.get(connection, workspace_id)
        assert found is not None
        return found.watch_state, found.watch_detail


async def due_scans(engine: AsyncEngine, workspace_id: UUID) -> list[dict[str, Any]]:
    """Queued scans for this workspace that a worker would claim right now."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(operation.c.id, operation.c.payload, operation.c.idempotency_key)
                .where(
                    operation.c.kind == "workspace.scan",
                    operation.c.subject_id == workspace_id,
                    operation.c.state == "queued",
                    operation.c.next_due_at <= func.now(),
                )
                .order_by(operation.c.created_at)
            )
        ).mappings()
        return [dict(row) for row in rows]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.fr("F-001/FR-21")
async def test_a_file_appearing_on_the_storage_hastens_its_scan(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """The whole point, end to end: drop a file in, and the scan that finds it runs now.

    The scan itself is the ordinary one — same operation, same handler — so what is asserted here
    is only that the doorbell reached it, with `watcher` recorded as the reason.
    """
    tree = tmp_path / "nas"
    build(tree, {"Photos/beach.jpg": b"a photo"})
    settings = watching_settings(identity_settings)

    async with adopt(settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, settings)

        async with running(engine, settings):
            await until(
                lambda: state_is(engine, workspace, "watching"),
                what="the root was not watched",
            )
            (tree / "Photos" / "later.jpg").write_bytes(b"copied in by hand")

            expedited = await until(lambda: due_scans(engine, workspace), what="no scan became due")

        assert [row["payload"]["trigger"] for row in expedited] == ["watcher"]
        assert [row["payload"]["path"] for row in expedited] == ["Photos"], (
            "the burst did not name the directory it happened in"
        )

        await scan_pending(identity_database, settings)
        report = await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}/import-status")

    assert report.json()["watch"]["state"] == "unwatched", "the watcher stopped with the block"
    latest = report.json()["recent"][0]
    assert latest["trigger"] == "watcher"
    assert latest["files_registered"] == 1
    async with engine.connect() as connection:
        assert (await connection.execute(select(file.c.name))).scalars().all() == [
            "beach.jpg",
            "later.jpg",
        ]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.fr("F-001/FR-21")
async def test_the_apps_own_staging_writes_ring_no_bell(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """An upload writes chunks into `.workspace/staging/` — and would otherwise keep the watcher
    busy for as long as it runs, scanning a tree that has not changed."""
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"a file"})
    settings = watching_settings(identity_settings)

    async with adopt(settings, identity_database, tree) as (_client, workspace):
        await scan_pending(identity_database, settings)

        async with running(engine, settings):
            await until(
                lambda: state_is(engine, workspace, "watching"),
                what="the root was not watched",
            )
            staging = tree / names.CONTROL_DIRECTORY / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            for index in range(5):
                (staging / f"chunk-{index}.partial").write_bytes(b"bytes in flight")
            # Long enough for a burst to have gone quiet twice over, had there been one.
            await asyncio.sleep(QUIET * 8)

            assert await due_scans(engine, workspace) == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.fr("F-001/FR-21")
async def test_a_root_the_kernel_will_not_watch_falls_back_to_the_schedule(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """The failure that will really happen: a tree larger than `fs.inotify.max_user_watches`.

    It is not an error. The workspace keeps its schedule, the worker keeps working, and the
    reason is on the row where someone can read it — with the fix named.
    """
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"a file"})
    settings = watching_settings(identity_settings)

    class Refusing(watcher.Observer):  # type: ignore[misc, valid-type]
        def schedule(self, *arguments: Any, **keywords: Any) -> Any:
            raise OSError(28, "inotify watch limit reached")

    async with adopt(settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, settings)

        async with running(engine, settings, observer=Refusing) as watching:
            recorded = await until(
                lambda: state_is(engine, workspace, "unavailable"),
                what="the refusal was not recorded",
            )
            assert watching.watching == set(), "a refused watch was kept anyway"

        assert "fs.inotify.max_user_watches" in (recorded[1] or "")

        # And the scheduled pass still does its job.
        (tree / "later.txt").write_bytes(b"found the slow way")
        response = await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan",
            json={},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 202, response.text
        await scan_pending(identity_database, settings)

    async with engine.connect() as connection:
        assert sorted((await connection.execute(select(file.c.name))).scalars().all()) == [
            "later.txt",
            "notes.txt",
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_disabled_watcher_does_nothing_at_all(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Off is a supported configuration, and every other scan test runs in it."""
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"a file"})
    settings = watching_settings(identity_settings, watcher_enabled=False)

    def refuse() -> BaseObserver:  # pragma: no cover - must never be called
        raise AssertionError("a disabled watcher started an observer")

    async with adopt(settings, identity_database, tree) as (_client, workspace):
        await scan_pending(identity_database, settings)
        async with running(engine, settings, observer=refuse):
            await asyncio.sleep(QUIET * 4)
            (tree / "later.txt").write_bytes(b"nobody is listening")
            await asyncio.sleep(QUIET * 4)

        assert await watch_state(engine, workspace) == ("unwatched", None)
        assert await due_scans(engine, workspace) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_workspace_that_goes_away_is_let_go(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """Subscriptions are re-derived from the database, so the set follows what exists."""
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"a file"})
    settings = watching_settings(identity_settings)

    async with (
        adopt(settings, identity_database, tree) as (_client, workspace),
        running(engine, settings) as watching,
    ):
        await until(lambda: watching.watching, what="the root was not watched")

        async with engine.connect() as connection:
            await connection.execute(
                delete(workspace_table).where(workspace_table.c.id == workspace)
            )
            await connection.commit()

        assert await until(lambda: not watching.watching, what="the watch outlived its workspace")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.fr("F-001/FR-21")
async def test_a_scan_of_a_directory_the_index_has_not_seen_scans_its_parent(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """A watcher event for a folder created a moment ago names a path no folder row holds yet.

    Refusing it would dead-letter the operation for the one case the watcher exists to handle, so
    the scan starts from the nearest folder it does know — the parent that discovers it. Driven
    through the queue directly, because the timing this depends on is "the event arrived before
    the scan did", not the observer.
    """
    tree = tmp_path / "nas"
    build(tree, {"notes.txt": b"a file"})
    settings = watching_settings(identity_settings)

    async with adopt(settings, identity_database, tree) as (client, workspace):
        await scan_pending(identity_database, settings)
        (tree / "Album").mkdir()
        (tree / "Album" / "new.jpg").write_bytes(b"arrived with its folder")

        async with create_async_engine(identity_database).connect() as connection:
            queued = await scans.ensure_scheduled(
                connection,
                workspace_id=workspace,
                due_in=timedelta(0),
                trigger="watcher",
                path="Album",
            )
            await operations.expedite(
                connection,
                operation_id=queued.id,
                payload=scans.request_payload(trigger="watcher", path="Album", requested_by=None),
            )
            await connection.commit()

        await scan_pending(identity_database, settings)
        report = await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}/import-status")

    latest = report.json()["recent"][0]
    assert latest["state"] == "completed", "an unknown subtree stopped the scan"
    assert latest["path"] == "", "it should have fallen back to the folder it knows"
    assert latest["files_registered"] == 1
