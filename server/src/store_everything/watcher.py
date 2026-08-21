"""The filesystem watcher: a doorbell in front of the scan schedule.

[ADR-0019](../../../decisions/ADR-0019-source-tree-semantics.md) decided what this is allowed to
be, and it is deliberately weak: **a watcher event only ever hastens a scan the schedule would
have run anyway.** Nothing here may be load-bearing — it is a lossy doorbell in
[12](../../../specs/12-reliability.md#durable-schedules-lossy-doorbells)'s sense — because kernel
events do not fire at all for changes made on the server side of an SMB or NFS mount, and a tree
can be larger than the kernel is willing to watch. So every failure in this module degrades to
"the hourly pass will find it" — and says so, on the workspace's row.

The shape follows from that:

- **Events coalesce at intake, in the observer's own thread.** A copy of five hundred files is
  one intention; a `find -exec touch` over a million is still one. Each workspace keeps a single
  *burst* holding the deepest directory that covers everything seen so far, so unbounded events
  cost CPU and never memory — there is no queue to overflow.
- **A burst becomes work when it goes quiet** (`SE_WATCHER_DEBOUNCE_SECONDS`), or when it has
  been running for `MAX_HOLD_FACTOR` times that, so an `rsync` writing for an hour is scanned
  while it runs rather than only when it stops.
- **The work is the ordinary rescan**: `ensure_scheduled` + `expedite`, exactly what the manual
  rescan endpoint does, with `trigger="watcher"` and nobody to attribute it to. There is no
  watcher-specific path through the scanner at all.
- **`.workspace/` is ignored**, which is not cosmetic: every upload chunk lands in
  `.workspace/staging/`, so without that filter the watcher would fire on our own writes for as
  long as an upload runs.

The app cannot feed its own watcher beyond that: what it writes inside a root is the file the
user asked for, and everything else it writes — `versions/`, the derived store — lives on the app
volume, which is not a workspace root.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from store_everything import names, operations, scans, workspaces
from store_everything.config import Settings

_logger = logging.getLogger(__name__)

#: A burst that never goes quiet is acted on anyway, this many debounce windows in. Not a
#: setting: it is a consequence of the debounce rather than an independent knob.
MAX_HOLD_FACTOR = 12

#: How often the set of watched roots is re-derived, so a workspace created a minute ago starts
#: being watched without a restart.
RESUBSCRIBE_SECONDS = 60.0

#: The workspace root itself, as the scan's payload spells a subtree.
WHOLE_WORKSPACE = scans.ROOT


def subtree_for(root: Path, changed: Path, *, is_directory: bool) -> str | None:
    """The workspace-relative directory to rescan for a change at this path, or `None`.

    A scan works in directories, so an event has to name one: for a file, the directory holding
    it; for a directory, itself, because "something in here changed" is what such an event means.
    A directory that is *new* names a path the index has not seen — the scan falls back to the
    deepest folder it does know, which is the parent that will discover it.

    `None` means the event is not ours to act on: outside the root, or inside the control
    directory the app owns.
    """
    try:
        relative = changed.relative_to(root)
    except ValueError:
        return None
    if names.CONTROL_DIRECTORY in relative.parts:
        return None
    parts = relative.parts if is_directory else relative.parts[:-1]
    return "/".join(parts)


def common_ancestor(first: str, second: str) -> str:
    """The deepest directory containing both — how a burst spanning folders collapses to one."""
    if first == second:
        return first
    shared: list[str] = []
    for left, right in zip(first.split("/"), second.split("/"), strict=False):
        if left != right:
            break
        shared.append(left)
    return "/".join(shared)


@dataclass(slots=True)
class Burst:
    """What one workspace's events have added up to, and when to act on them."""

    subtree: str
    first_at: float
    last_at: float

    def note(self, subtree: str, at: float) -> None:
        self.subtree = common_ancestor(self.subtree, subtree)
        self.last_at = at

    def is_due(self, now: float, *, quiet: float, max_hold: float) -> bool:
        """Quiet for long enough, or held for too long — the second is what covers an `rsync`
        that keeps writing for an hour and would otherwise never look quiet."""
        return (now - self.last_at) >= quiet or (now - self.first_at) >= max_hold


@dataclass(slots=True)
class Bursts:
    """Every workspace's pending burst. Written from the observer thread, read from the loop.

    A plain lock rather than an async queue: the observer thread must never be able to make the
    event loop's backlog grow without bound, and coalescing at intake means there is nothing to
    queue — one small object per workspace, however many events arrive.
    """

    now: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: dict[UUID, Burst] = field(default_factory=dict)

    def note(self, workspace_id: UUID, subtree: str) -> None:
        at = self.now()
        with self._lock:
            burst = self._pending.get(workspace_id)
            if burst is None:
                self._pending[workspace_id] = Burst(subtree, at, at)
            else:
                burst.note(subtree, at)

    def take_due(self, *, quiet: float, max_hold: float) -> list[tuple[UUID, str]]:
        """Remove and return the bursts that are ready to become scans."""
        at = self.now()
        with self._lock:
            due = [
                (workspace_id, burst.subtree)
                for workspace_id, burst in self._pending.items()
                if burst.is_due(at, quiet=quiet, max_hold=max_hold)
            ]
            for workspace_id, _ in due:
                del self._pending[workspace_id]
        return due

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def forget(self, workspace_id: UUID) -> None:
        with self._lock:
            self._pending.pop(workspace_id, None)


class _Handler(FileSystemEventHandler):
    """One workspace's events, turned into notes on its burst. Runs in the observer's thread."""

    def __init__(self, workspace_id: UUID, root: Path, bursts: Bursts) -> None:
        self._workspace_id = workspace_id
        self._root = root
        self._bursts = bursts

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Both ends of a move: the file left one directory and arrived in another, and each of
        # them has to be looked at again.
        for raw in (event.src_path, getattr(event, "dest_path", "")):
            if not raw:
                continue
            subtree = subtree_for(self._root, Path(str(raw)), is_directory=event.is_directory)
            if subtree is not None:
                self._bursts.note(self._workspace_id, subtree)


@dataclass(frozen=True, slots=True)
class Subscription:
    workspace_id: UUID
    root: Path
    watch: Any
    """watchdog's handle for this watch, kept only to unschedule it."""


class Watcher:
    """Subscribes to every active workspace root and expedites scans for what changes.

    Runs in the worker process beside the operation loop, so the API container watches nothing
    and one `SIGTERM` stops both. Created with an observer factory so a test can hand it a
    failing one — the failure path matters more than the happy one here.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        *,
        observer: Callable[[], BaseObserver] = Observer,
        now: Callable[[], float] = time.monotonic,
        resubscribe_seconds: float = RESUBSCRIBE_SECONDS,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._make_observer = observer
        self._observer: BaseObserver | None = None
        self._bursts = Bursts(now=now)
        self._subscriptions: dict[UUID, Subscription] = {}
        self._stopping = asyncio.Event()
        self._quiet = settings.watcher_debounce_seconds
        self._max_hold = settings.watcher_debounce_seconds * MAX_HOLD_FACTOR
        self._resubscribe_seconds = resubscribe_seconds

    @property
    def watching(self) -> set[UUID]:
        """Which workspaces this watcher currently holds a subscription for."""
        return set(self._subscriptions)

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        """Watch until stopped. Never raises: a broken watcher costs latency, not correctness."""
        if not self._settings.watcher_enabled:
            _logger.info("watcher disabled; external changes surface on the scan schedule")
            return

        await self._clear_stale_claims()
        self._observer = self._make_observer()
        self._observer.start()
        _logger.info(
            "watcher started",
            extra={"debounce_seconds": self._quiet, "max_hold_seconds": self._max_hold},
        )
        try:
            await self._loop()
        finally:
            await self._shutdown()

    async def _loop(self) -> None:
        resubscribed_at = 0.0
        # Half the quiet window, so a burst is acted on within about one and a half of them —
        # bounded either way, because an idle wake-up is one dict check under a lock.
        tick = min(max(self._quiet / 2, 0.25), 5.0)
        while not self._stopping.is_set():
            if (time.monotonic() - resubscribed_at) >= self._resubscribe_seconds:
                await self._resubscribe()
                resubscribed_at = time.monotonic()
            await self._expedite_due()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=tick)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ subscriptions

    async def _resubscribe(self) -> None:
        """Watch every active workspace, and stop watching the ones that are gone."""
        try:
            async with self._engine.connect() as connection:
                active = await workspaces.list_active(connection)
        except SQLAlchemyError as unreachable:
            # The database is the source of the list; without it there is nothing to change.
            _logger.warning("watcher could not list workspaces", extra={"error": str(unreachable)})
            return

        current = {found.id: found for found in active}
        for workspace_id in set(self._subscriptions) - set(current):
            self._unsubscribe(workspace_id)
        for found in active:
            if found.id not in self._subscriptions:
                await self._subscribe(found)

    async def _subscribe(self, found: workspaces.Workspace) -> None:
        """Start watching one root, or record why this workspace has only the schedule.

        A failure part-way through is torn down rather than left in place: watchdog adds a watch
        per directory as it walks the tree, so a kernel limit reached half-way would leave some
        directories firing and others silent — a state nobody could reason about. One honest
        answer instead: this workspace is not watched, and here is why.
        """
        observer = self._observer
        if observer is None:  # pragma: no cover - only reachable before `run_forever`
            return
        handler = _Handler(found.id, found.root_path, self._bursts)
        try:
            watch = await asyncio.to_thread(
                observer.schedule, handler, str(found.root_path), recursive=True
            )
        except (OSError, RuntimeError) as refused:
            reason = _reason(refused)
            _logger.warning(
                "watcher could not watch a workspace root; its schedule still covers it",
                extra={"workspace": str(found.id), "root": str(found.root_path), "error": reason},
            )
            await self._record(found.id, "unavailable", reason)
            return

        self._subscriptions[found.id] = Subscription(found.id, found.root_path, watch)
        await self._record(found.id, "watching", None)

    def _unsubscribe(self, workspace_id: UUID) -> None:
        subscription = self._subscriptions.pop(workspace_id, None)
        self._bursts.forget(workspace_id)
        if subscription is None or self._observer is None:
            return
        try:
            self._observer.unschedule(subscription.watch)
        except (KeyError, OSError, RuntimeError):  # pragma: no cover - already gone
            _logger.debug("watch was already released", extra={"workspace": str(workspace_id)})

    # ------------------------------------------------------------------ the doorbell

    async def _expedite_due(self) -> None:
        """Turn every burst that is ready into the ordinary rescan operation."""
        due = self._bursts.take_due(quiet=self._quiet, max_hold=self._max_hold)
        if not due:
            return
        try:
            async with self._engine.connect() as connection:
                for workspace_id, subtree in due:
                    await self._request_scan(connection, workspace_id, subtree)
                await connection.commit()
        except SQLAlchemyError as failed:
            # The schedule reaches the same state, so a lost doorbell press is not worth a
            # retry queue — and the next event will press it again.
            _logger.warning("watcher could not request a scan", extra={"error": str(failed)})

    async def _request_scan(self, connection: Any, workspace_id: UUID, subtree: str) -> None:
        reason = scans.request_payload(trigger="watcher", path=subtree, requested_by=None)
        queued = await scans.ensure_scheduled(
            connection,
            workspace_id=workspace_id,
            due_in=timedelta(0),
            trigger="watcher",
            path=subtree,
        )
        # A pending scan for the same subtree — the hourly one, or another burst's — is the row
        # this converges on, so it has to be pulled forward and told why it is running.
        await operations.expedite(connection, operation_id=queued.id, payload=reason)
        _logger.info(
            "watcher expedited a scan",
            extra={
                "workspace": str(workspace_id),
                "path": subtree or WHOLE_WORKSPACE,
                "operation": str(queued.id),
            },
        )

    # ------------------------------------------------------------------ housekeeping

    async def _record(
        self, workspace_id: UUID, state: workspaces.WatchState, detail: str | None
    ) -> None:
        try:
            async with self._engine.connect() as connection:
                await workspaces.record_watch(
                    connection, workspace_id=workspace_id, state=state, detail=detail
                )
                await connection.commit()
        except SQLAlchemyError as failed:  # pragma: no cover - reporting is not load-bearing
            _logger.debug("watcher could not record its state", extra={"error": str(failed)})

    async def _clear_stale_claims(self) -> None:
        """Nobody is watching yet, whatever the rows say after an unclean stop."""
        try:
            async with self._engine.connect() as connection:
                cleared = await workspaces.clear_watches(connection)
                await connection.commit()
        except SQLAlchemyError as failed:  # pragma: no cover - reporting is not load-bearing
            _logger.debug("watcher could not clear stale state", extra={"error": str(failed)})
            return
        if cleared:
            _logger.debug("cleared stale watch state", extra={"workspaces": cleared})

    async def _shutdown(self) -> None:
        for workspace_id in list(self._subscriptions):
            self._unsubscribe(workspace_id)
        observer = self._observer
        self._observer = None
        if observer is not None:
            observer.stop()
            await asyncio.to_thread(observer.join)
        await self._clear_stale_claims()
        _logger.info("watcher stopped")


def _reason(failure: BaseException) -> str:
    """A one-line explanation an operator can act on, with the fix named where there is one."""
    text = str(failure) or failure.__class__.__name__
    if isinstance(failure, OSError) and failure.errno == 28:  # ENOSPC
        return (
            f"{text} — the kernel's watch limit is reached; raise "
            "fs.inotify.max_user_watches or set SE_WATCHER_ENABLED=false. The scheduled scan "
            "still covers this workspace."
        )
    return f"{text} — the scheduled scan still covers this workspace."
