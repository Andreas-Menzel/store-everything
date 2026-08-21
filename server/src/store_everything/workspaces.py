"""Workspaces: where a user's files live, and what it takes to admit a directory.

A workspace is the top-level container of files, owned by exactly one user
([02 § workspace](../../../specs/02-domain-model.md#workspace)). Every `local` workspace has
a root directory on disk plus a **placement** saying who chose that path (ADR-0018):

- `managed` — created by us under `SE_DATA_ROOT`, at `users/{owner}/workspaces/{name}/data`.
  The path is ours to shape, so the directory carries the workspace's name.
- `adopted` — an existing directory, indexed in place with nothing moved or copied. This is
  the 10 TB-NAS story, and it is **admin-only** and confined to the `SE_ADOPTION_ROOTS`
  allow-list: members never submit filesystem paths.

Everything downstream — scanning, folders, uploads, versions — treats the two identically.

Three things are checked before a directory becomes a workspace root, and all three refuse
rather than repair:

1. **Containment.** An adopted candidate is `realpath`-resolved and must land inside the
   allow-list, outside the app-owned areas, and must neither contain nor be contained by
   another workspace root. Lexical containment is not containment (ADR-0019).
2. **The filesystem.** `fs-check` exercises atomic rename and honest `fsync` directly; a root
   whose filesystem fails is refused naming the property that failed, and the verdict is
   *recorded* on the workspace so a later surprise has evidence.
3. **The name.** Unique per owner on the comparison key (`names`), because for a managed
   placement the name is also a path segment.

Creation is split across two transactions on purpose (ADR-0010): the request writes the row
and the intent to provision, and the `workspace.provision` operation creates the directory,
plants the control directory and registers the root folder. A crash in between leaves a
workspace that is still `provisioning` and an operation that will be claimed again — never a
row pointing at a directory that does not exist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Select, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, filestore, folders, fscheck, names, scans, workspacefs
from store_everything.config import Settings
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.runner import Job, PermanentFailureError
from store_everything.tables import workspace

#: The operation that turns a requested workspace into a usable one.
KIND = "workspace.provision"

type Source = Literal["local"]
type Placement = Literal["managed", "adopted"]
type State = Literal["provisioning", "active"]

#: An arbitrary constant; its only requirement is that nothing else in this schema uses it.
_ROOT_ADMISSION_LOCK = 1_837_465_001


class AdoptionRefusedError(Exception):
    """A candidate directory may not be adopted.

    Carries the reason as prose, because the operator reading it has to act on it: an
    allow-list to extend, a mount to fix, a workspace that already covers the path.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    owner_id: UUID
    name: str
    source: Source
    placement: Placement
    root_path: Path
    state: State
    fs_check: dict[str, Any]
    fs_checked_at: datetime
    scan_interval_minutes: int
    created_at: datetime

    @property
    def is_active(self) -> bool:
        return self.state == "active"


# ------------------------------------------------------------------- placement policy


def managed_root(settings: Settings, *, owner_id: UUID, name: str) -> Path:
    """Where a managed workspace's files live (03 § storage layout). **Blocking.**

    The workspace's own directory carries its **name** so that someone browsing the storage
    without the app recognises what they are looking at — the point of the portability
    promise. The owner's directory carries their **id**: an email address is neither
    length-bounded nor guaranteed free of `/`, and a display name is not unique.

    A submitted name is only safe here because `names.validate_name` has already refused `/`,
    `.`, `..` and control characters — and `resolve_within` re-checks the result anyway,
    which is also what turns a symlinked `SE_DATA_ROOT` into the path we will actually open.
    """
    candidate = settings.data_root / "users" / str(owner_id) / "workspaces" / name / "data"
    return filestore.resolve_within(settings.data_root, candidate)


def _overlaps(first: Path, second: Path) -> bool:
    """Whether two roots are the same or one is inside the other."""
    return first == second or first in second.parents or second in first.parents


def resolve_adopted_root(settings: Settings, candidate: str) -> Path:
    """Resolve a submitted path and refuse it unless it may be adopted. **Blocking.**

    Refusal is the only outcome besides a resolved path: this is the function that stands
    between a request field and the filesystem, so every branch that is not "allowed" raises.
    """
    if not settings.adoption_roots:
        raise AdoptionRefusedError(
            "adoption is disabled on this instance (SE_ADOPTION_ROOTS is empty)"
        )
    if len(candidate.encode()) > names.MAX_PATH_BYTES:
        raise AdoptionRefusedError(f"a path must be at most {names.MAX_PATH_BYTES} bytes")

    path = Path(candidate)
    if not path.is_absolute():
        raise AdoptionRefusedError("a path to adopt must be absolute")

    # Resolved before every comparison, because a symlink whose *path* is inside the
    # allow-list can have a target that is not — the File Browser CVE, exactly.
    resolved = path.resolve()
    permitted = any(
        resolved == allowed or allowed in resolved.parents
        for allowed in (root.resolve() for root in settings.adoption_roots)
    )
    if not permitted:
        raise AdoptionRefusedError("that path is not inside SE_ADOPTION_ROOTS")
    if not resolved.is_dir():
        raise AdoptionRefusedError("that path is not a directory")

    # The app-owned areas must stay outside every workspace root (ADR-0018), and the managed
    # root is where directories we create appear — an adopted root overlapping either would
    # make one tree two things.
    for reserved, label in (
        (settings.data_root, "SE_DATA_ROOT"),
        (settings.app_data_root, "SE_APP_DATA_ROOT"),
    ):
        if _overlaps(resolved, reserved.resolve()):
            raise AdoptionRefusedError(f"that path overlaps {label}")
    return resolved


def preflight(settings: Settings, *, placement: Placement, root: Path) -> fscheck.Verdict:
    """Probe the filesystem that will hold this workspace, before its row exists. **Blocking.**

    For an adopted placement the root itself is probed. For a managed one the root does not
    exist yet, so the probe runs against `SE_DATA_ROOT` — the same filesystem, because we
    create the path inside it. The provisioning run probes the real root afterwards and its
    verdict replaces this one, so what a workspace *records* always describes its own root.
    """
    if placement == "adopted":
        return fscheck.probe(root)
    filestore.ensure_directory(settings.data_root)
    return fscheck.probe(settings.data_root)


# ---------------------------------------------------------------------- data access

_COLUMNS = (
    workspace.c.id,
    workspace.c.owner_id,
    workspace.c.name,
    workspace.c.source,
    workspace.c.placement,
    workspace.c.root_path,
    workspace.c.state,
    workspace.c.fs_check,
    workspace.c.fs_checked_at,
    workspace.c.scan_interval_minutes,
    workspace.c.created_at,
)

type _Row = tuple[
    UUID, UUID, str, Source, Placement, str, State, dict[str, Any], datetime, int, datetime
]


def _as_workspace(row: _Row) -> Workspace:
    (
        identifier,
        owner_id,
        name,
        source,
        placement,
        root,
        state,
        verdict,
        checked_at,
        interval,
        created,
    ) = row
    return Workspace(
        id=identifier,
        owner_id=owner_id,
        name=name,
        source=source,
        placement=placement,
        # Stored as text and used as a path: converting here means nothing downstream has to
        # remember which of the two it is holding.
        root_path=Path(root),
        state=state,
        fs_check=verdict,
        fs_checked_at=checked_at,
        scan_interval_minutes=interval,
        created_at=created,
    )


def _query() -> Select[_Row]:
    return select(*_COLUMNS)


async def lock_admission(connection: AsyncConnection) -> None:
    """Serialize workspace-root admission for the rest of this transaction.

    "No root overlaps another" is a relation between two rows, which no constraint can
    express — so two concurrent requests could both find no overlap and both insert. The
    lock is cheap (workspace creation is rare and human-paced) and it makes the check that
    follows it mean what it says. Exact duplicates are caught by the unique constraint even
    without it.
    """
    await connection.execute(select(func.pg_advisory_xact_lock(_ROOT_ADMISSION_LOCK)))


async def conflicting_root(connection: AsyncConnection, root: Path) -> Workspace | None:
    """An existing workspace whose root contains, is contained by, or equals `root`.

    Compared in Python over every row rather than with a SQL prefix match: `LIKE '/a/b%'`
    would also match `/a/bc`, and the number of workspaces on an instance of 1 to 30 users is
    small enough that exactness is free.
    """
    for row in (await connection.execute(_query())).all():
        found = _as_workspace(tuple(row))
        if _overlaps(root, found.root_path):
            return found
    return None


async def get(connection: AsyncConnection, workspace_id: UUID) -> Workspace | None:
    row = (await connection.execute(_query().where(workspace.c.id == workspace_id))).first()
    return None if row is None else _as_workspace(tuple(row))


async def find_by_name(
    connection: AsyncConnection, *, owner_id: UUID, name: str
) -> Workspace | None:
    """The owner's workspace of that name, comparing keys rather than raw names."""
    row = (
        await connection.execute(
            _query().where(
                workspace.c.owner_id == owner_id,
                workspace.c.name_key == names.comparison_key(name),
            )
        )
    ).first()
    return None if row is None else _as_workspace(tuple(row))


async def list_for_owner(
    connection: AsyncConnection,
    *,
    owner_id: UUID,
    limit: int,
    after: tuple[datetime, UUID] | None = None,
) -> list[Workspace]:
    """One user's workspaces in creation order, keyset-paginated."""
    query = (
        _query()
        .where(workspace.c.owner_id == owner_id)
        .order_by(workspace.c.created_at, workspace.c.id)
        .limit(limit)
    )
    if after is not None:
        created_at, identifier = after
        query = query.where(
            or_(
                workspace.c.created_at > created_at,
                and_(workspace.c.created_at == created_at, workspace.c.id > identifier),
            )
        )
    return [_as_workspace(tuple(row)) for row in (await connection.execute(query)).all()]


async def list_active(connection: AsyncConnection) -> list[Workspace]:
    """Every workspace whose storage exists. What the scan schedules are asserted over."""
    rows = (await connection.execute(_query().where(workspace.c.state == "active"))).all()
    return [_as_workspace(tuple(row)) for row in rows]


async def staging_roots(connection: AsyncConnection) -> tuple[Path, ...]:
    """Every workspace's own staging area, for the janitor and the audit.

    Workspace staging lives inside the user's tree because a commit has to be a rename on
    the destination filesystem (ADR-0018), so debris there is as much the janitor's business
    as debris on the app volume — and nobody else knows these paths.
    """
    roots = (await connection.execute(select(workspace.c.root_path))).scalars().all()
    return tuple(workspacefs.staging_directory(Path(root)) for root in roots)


async def create(
    connection: AsyncConnection,
    *,
    owner_id: UUID,
    name: str,
    placement: Placement,
    root_path: Path,
    verdict: fscheck.Verdict,
    scan_interval_minutes: int,
    actor: Actor,
) -> Workspace:
    """Record a workspace and the verdict that admitted it. Its directory follows.

    The row lands in `provisioning`: it is the durable intent, and the caller enqueues the
    operation that makes it true in this same transaction.
    """
    row = (
        await connection.execute(
            insert(workspace)
            .values(
                id=new_id(),
                owner_id=owner_id,
                name=name,
                name_key=names.comparison_key(name),
                source="local",
                placement=placement,
                root_path=str(root_path),
                state="provisioning",
                fs_check=verdict.as_record(),
                scan_interval_minutes=scan_interval_minutes,
            )
            .returning(*_COLUMNS)
        )
    ).one()
    created = _as_workspace(tuple(row))

    await events.record(
        connection,
        action=events.WORKSPACE_CREATED,
        resource_type=events.RESOURCE_WORKSPACE,
        resource_id=created.id,
        actor=actor,
        details={
            "name": created.name,
            "owner": str(created.owner_id),
            "placement": created.placement,
            # The path as it was at creation time, so the audit trail stays readable after a
            # rename or a re-mount (F-011/FR-9).
            "root": str(created.root_path),
        },
    )
    return created


async def mark_active(
    connection: AsyncConnection, *, workspace_id: UUID, verdict: fscheck.Verdict, actor: Actor
) -> Workspace | None:
    """Flip a provisioned workspace to `active`, recording the verdict for its real root.

    Guarded on the current state, so a second provisioning attempt cannot re-announce a
    workspace that is already usable.
    """
    row = (
        await connection.execute(
            update(workspace)
            .where(workspace.c.id == workspace_id, workspace.c.state == "provisioning")
            .values(
                state="active",
                fs_check=verdict.as_record(),
                fs_checked_at=func.now(),
                updated_at=func.now(),
            )
            .returning(*_COLUMNS)
        )
    ).first()
    if row is None:
        return None

    activated = _as_workspace(tuple(row))
    await events.record(
        connection,
        action=events.WORKSPACE_PROVISIONED,
        resource_type=events.RESOURCE_WORKSPACE,
        resource_id=activated.id,
        actor=actor,
        details={"root": str(activated.root_path), "placement": activated.placement},
    )
    return activated


# --------------------------------------------------------------------- provisioning


def _materialize(record: Workspace, operation_id: UUID) -> fscheck.Verdict:
    """Create what the placement requires, probe it, plant the control directory. **Blocking.**

    A managed root is ours to create. An adopted one must already be there: creating it would
    turn "the share is not mounted" into "the workspace is empty", and an empty workspace is
    what a scan would then reconcile against — deleting every file it knows about.
    """
    if record.placement == "managed":
        filestore.ensure_directory(record.root_path)

    verdict = fscheck.probe(record.root_path)
    if verdict.usable:
        workspacefs.materialize(
            record.root_path,
            workspace_id=record.id,
            placement=record.placement,
            created_at=record.created_at,
            operation_id=operation_id,
        )
    return verdict


async def provision(job: Job) -> dict[str, Any]:
    """Make a requested workspace real: root directory, control directory, root folder.

    Idempotent in every step, because this is a leased operation that a crash can replay:
    directory creation tolerates existence, the marker is rewritten, the root folder converges
    on the one that is already there, and the activation is guarded on the current state.
    """
    workspace_id = job.operation.subject_id
    if workspace_id is None:
        raise PermanentFailureError(f"{KIND} needs a workspace as its subject")

    found = await get(job.connection, workspace_id)
    if found is None:
        # Nothing to converge on, and nothing to resurrect: a workspace only disappears by
        # being removed deliberately.
        return {"workspace": str(workspace_id), "outcome": "gone"}
    if found.is_active:
        return {"workspace": str(found.id), "outcome": "already-active"}

    verdict = await asyncio.to_thread(_materialize, found, job.operation.id)
    if not verdict.usable:
        # The probe passed when the workspace was requested, so this is the world changing
        # under us — an unmounted share, a read-only remount, a full disk. Retryable on
        # purpose: dead-lettering after the configured attempts is what makes it visible.
        raise OSError(f"the workspace root is not usable: {verdict.explain()}")

    root_folder, _ = await folders.create_root(
        job.connection, workspace_id=found.id, actor=Actor.system()
    )
    # A reclaimed lease can put two runs on one workspace; the guarded transition decides
    # which of them announced it, and the result says which this was.
    activated = await mark_active(
        job.connection, workspace_id=found.id, verdict=verdict, actor=Actor.system()
    )
    # The import (F-001/FR-4): an adopted tree already has files in it, and a managed one is
    # empty — the same first scan covers both, and it is due immediately.
    await scans.ensure_scheduled(job.connection, workspace_id=found.id, trigger="initial")
    return {
        "workspace": str(found.id),
        "outcome": "provisioned" if activated is not None else "already-active",
        "root": str(found.root_path),
        "root_folder": str(root_folder.id),
    }
