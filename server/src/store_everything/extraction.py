"""Routing files to extractors, and the life of one job.

Two things live here, because they are two halves of one sentence — *this version needs
processing, and here is what happened when it was*:

- **routing** (04 § detection/routing): every path that creates a file version converges on
  `route()`, which matches the version against the registered manifests and writes, per match,
  one `operation` row (the queue) and one `extraction_run` row (the record) sharing an id. It
  runs inside the transaction that created the version, so "the row that says the file exists
  commits with the row that says extract it";
- **the job's transitions**, driven by the extractor over the wire (ADR-0020): claim → running,
  heartbeat, then succeeded or failed. Every one of them is the operation layer's transition
  plus the run's, in one transaction, so the queue and the record cannot disagree.

What is deliberately *not* here: an event per job. Queueing work was never an audited action in
this system (a scan, a rollup and a janitor sweep all queue silently), and a successful run is
a row in `extraction_run` — stamped, joined to its outputs, and outliving the queue. Failures
still reach the log through the operation layer's own `operation.dead_lettered`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import operations, results, tagging
from store_everything.derived import DerivedStore
from store_everything.extractors import Manifest
from store_everything.ids import new_id
from store_everything.operations import PRIORITY_PRESENCE, PRIORITY_SEARCHABILITY, Operation
from store_everything.tables import (
    FIRST_GENERATION,
    TERMINAL_OPERATION_STATES,
    derived_asset,
    extraction_run,
    extractor,
    file_auto_tag,
    file_version,
    metadata_entry,
    operation,
    segment,
)

_logger = logging.getLogger(__name__)

#: Operation kinds are per extractor, so the claim query can filter to one extractor's work
#: without a payload predicate — and a worker build that has no handler for `extract.*` never
#: claims one by accident (the in-process runner claims only kinds it can run).
KIND_PREFIX = "extract."

#: How many attempts a job gets before it dead-letters. The operation layer's default is a
#: setting; extraction pins its own so that a poison file cannot be retried forever on an
#: instance whose queue is tuned generously (12 § tuning defaults).
MAX_ATTEMPTS = 4

#: Output kinds that only make a file *present* — browsable, with a thumbnail. Everything else
#: makes it findable, which is the slower and more important half (04 § prioritization).
_PRESENCE_OUTPUTS = frozenset({"metadata", "derived_assets"})

type Status = Literal["none", "pending", "indexed", "partial", "failed"]


def kind_of(extractor_id: str) -> str:
    return f"{KIND_PREFIX}{extractor_id}"


def extractor_of(kind: str) -> str | None:
    """The extractor a job kind belongs to, or `None` if it is not an extraction job."""
    return kind[len(KIND_PREFIX) :] if kind.startswith(KIND_PREFIX) else None


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of one extractor over one file version."""

    id: UUID
    extractor_id: str
    file_version_id: UUID
    generation: int
    state: str
    extractor_version: str | None
    model_version: str | None
    input_asset_id: UUID | None
    """The derived asset this run is about, or `None` for the file's own bytes."""

    reused_from: UUID | None
    """Set when these rows were copied from a run over byte-identical content (F-009/FR-8)."""

    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_OPERATION_STATES


@dataclass(frozen=True, slots=True)
class VersionFacts:
    """What an extractor is told about the version it is looking at."""

    id: UUID
    content_hash: str
    size_bytes: int
    media_type: str
    media_class: str
    is_current: bool
    file_id: UUID


@dataclass(frozen=True, slots=True)
class AssetFacts:
    """A derived asset an extractor has been given as its input."""

    id: UUID
    kind: str
    name: str
    media_type: str
    size_bytes: int
    content_hash: str
    source_hash: str
    """The content hash of the *version* it was derived from — which is where it lives on disk."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A claim: the job, its fencing token, and what it is about."""

    operation: Operation
    run: Run
    version: VersionFacts
    lease_expires_at: datetime
    input: AssetFacts | None = None
    """The derived asset to analyse, for a chained job. `None` means the file's own bytes."""


_RUN_COLUMNS = (
    extraction_run.c.id,
    extraction_run.c.extractor_id,
    extraction_run.c.file_version_id,
    extraction_run.c.generation,
    extraction_run.c.state,
    extraction_run.c.extractor_version,
    extraction_run.c.model_version,
    extraction_run.c.input_asset_id,
    extraction_run.c.reused_from,
    extraction_run.c.started_at,
    extraction_run.c.finished_at,
    extraction_run.c.error,
    extraction_run.c.created_at,
)

_VERSION_FACTS = (
    file_version.c.id,
    file_version.c.content_hash,
    file_version.c.size_bytes,
    file_version.c.media_type,
    file_version.c.media_class,
    file_version.c.is_current,
    file_version.c.file_id,
)


def _as_run(row: Sequence[Any]) -> Run:
    return Run(*row)  # pyright: ignore[reportArgumentType]


# ----------------------------------------------------------------------------------- routing


async def route(
    connection: AsyncConnection,
    *,
    file_version_id: UUID,
    media_type: str,
    content_hash: str | None = None,
    generation: int = FIRST_GENERATION,
) -> list[Run]:
    """Create the jobs this version needs. Returns the runs, whether new or already there.

    Called from three moments, and it has to be right in all of them: when a version is created,
    when a result lands (a new fact may satisfy a predicate, a new asset may need chaining), and
    when reprocessing asks for a generation. So it is **idempotent per *(version, extractor,
    generation, input)***: work that already has a run is skipped, whatever brought us here.

    Three ways a job comes to exist, in order of how much they cost:

    1. **Reuse** — someone already ran this extractor, at this version, over byte-identical
       content. The rows are copied and no job is created at all (F-009/FR-8).
    2. **The file's own bytes** — the manifest accepts this media type, and its `when` predicate
       (if any) is satisfied by what is already known about the version.
    3. **A derived input** — one job per matching asset, because a video's fifty keyframes are
       fifty pieces of work (12 § job atomicity).
    """
    # Keyed by *this* generation's work, which is what makes the idempotence above true: a run
    # from an earlier generation is history, not a reason to skip the reprocessing that was
    # asked for. Without the generation in the key, `route(generation=2)` would look at
    # generation 1's rows and conclude there was nothing to do (F-003/FR-6, F-009).
    existing = {
        (run.extractor_id, run.input_asset_id): run
        for run in await runs_for(connection, file_version_id)
        if run.generation == generation
    }
    assets = await _assets_of(connection, file_version_id)
    created: list[Run] = []

    for extractor_id, manifest in await _candidates(connection):
        wants_original = (extractor_id, None) not in existing and _accepts(manifest, media_type)
        if wants_original and await _predicate_holds(connection, manifest, file_version_id):
            reused = (
                None
                if content_hash is None
                else await _reuse(
                    connection,
                    extractor_id=extractor_id,
                    manifest=manifest,
                    file_version_id=file_version_id,
                    content_hash=content_hash,
                    generation=generation,
                )
            )
            created.append(
                reused
                or await _create_job(
                    connection,
                    extractor_id=extractor_id,
                    manifest=manifest,
                    file_version_id=file_version_id,
                    generation=generation,
                    params=await _params_from(connection, manifest, file_version_id),
                )
            )

        for asset_id, kind in assets:
            if kind in manifest.accepts.derived_kinds and (extractor_id, asset_id) not in existing:
                created.append(
                    await _create_job(
                        connection,
                        extractor_id=extractor_id,
                        manifest=manifest,
                        file_version_id=file_version_id,
                        generation=generation,
                        input_asset_id=asset_id,
                    )
                )

    return [*existing.values(), *created]


async def _assets_of(connection: AsyncConnection, file_version_id: UUID) -> list[tuple[UUID, str]]:
    """The derived assets of one version, as (id, kind) — the inputs chaining can offer."""
    rows = await connection.execute(
        select(derived_asset.c.id, derived_asset.c.kind)
        .where(derived_asset.c.file_version_id == file_version_id)
        .order_by(derived_asset.c.created_at, derived_asset.c.name)
    )
    return [(row[0], row[1]) for row in rows.all()]


async def _predicate_holds(
    connection: AsyncConnection, manifest: Manifest, file_version_id: UUID
) -> bool:
    """Whether `accepts.when` is satisfied by what is already known about this version.

    This is the whole of how `tesseract-ocr` learns that a PDF needs it: `pdf-text` writes
    `needs_ocr`, and the next routing pass — the one that runs when that result lands — finds the
    predicate satisfied. Neither extractor knows the other exists (04 § routing).
    """
    when = manifest.accepts.when
    if when is None:
        return True
    actual = await results.value_of(connection, file_version_id=file_version_id, key=when.key)
    return _equivalent(actual, when.equals)


def _equivalent(actual: Any, expected: bool | int | float | str) -> bool:
    """Comparison that refuses Python's `True == 1`, because a flag is not a count."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual is expected
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return float(actual) == float(expected)
    return actual == expected


async def _params_from(
    connection: AsyncConnection, manifest: Manifest, file_version_id: UUID
) -> dict[str, Any]:
    """The job parameters a manifest asks to be filled from well-known metadata.

    How `tesseract-ocr` is told *which* pages to read rather than re-deciding it: the extractor
    that found them wrote `ocr_pages`, and the manifest asks for it under the name its own code
    uses.
    """
    wanted = manifest.accepts.params_from
    if not wanted:
        return {}
    params: dict[str, Any] = {}
    for key, parameter in wanted.items():
        value = await results.value_of(connection, file_version_id=file_version_id, key=key)
        if value is not None:
            params[parameter] = value
    return params


async def _candidates(connection: AsyncConnection) -> list[tuple[str, Manifest]]:
    """Every extractor that could be routed work: registered, enabled, and still understood.

    A stored manifest is re-validated rather than read field by field, so there is exactly one
    interpretation of the contract in this codebase. The cost is a parse per registered
    extractor per new version, which is noise beside hashing the file's bytes.

    An extractor whose stored manifest this core no longer accepts — a rule tightened in an
    upgrade, say — is skipped with a warning instead of failing the upload that triggered
    routing. It re-registers on its next start-up, and until then its facet is missing, which
    is the contract's own promise about a broken extractor (05 § compatibility rules).
    """
    rows = await connection.execute(
        select(extractor.c.id, extractor.c.manifest).where(
            extractor.c.enabled, extractor.c.manifest.isnot(None)
        )
    )

    candidates: list[tuple[str, Manifest]] = []
    for extractor_id, document in rows.all():
        try:
            candidates.append((extractor_id, Manifest.model_validate(document)))
        except ValidationError:
            _logger.warning(
                "extractor %s has a stored manifest this version cannot read; not routing to it",
                extractor_id,
            )
    return candidates


def _accepts(manifest: Manifest, media_type: str) -> bool:
    """Whether this extractor takes an original of this media type.

    Only the media type is asked here; whether the extractor's precondition holds is a separate
    question with a separate answer (`_predicate_holds`), because one is about the file and the
    other about what has been learned of it so far.
    """
    return any(_matches(pattern, media_type) for pattern in manifest.accepts.mime_types)


def _matches(pattern: str, media_type: str) -> bool:
    if pattern == "*/*":
        return True
    if pattern.endswith("/*"):
        return media_type.partition("/")[0] == pattern[:-2]
    return pattern == media_type


def _priority(manifest: Manifest) -> int:
    """The core assigns priority from what an extractor produces; extractors never pick it."""
    return (
        PRIORITY_PRESENCE if set(manifest.produces) <= _PRESENCE_OUTPUTS else PRIORITY_SEARCHABILITY
    )


def idempotency_key(
    *,
    file_version_id: UUID,
    extractor_id: str,
    extractor_version: str,
    model_version: str | None,
    generation: int,
    input_asset_id: UUID | None = None,
) -> str:
    """Deterministic, so re-detecting the same work converges instead of duplicating it (05).

    The input is part of it, because a job over a keyframe is not the same work as a job over the
    file: without it, chaining would converge fifty keyframes onto one job.

    Readable rather than hashed: this string turns up in the operation table when somebody is
    working out why a job exists, and a digest would answer that question with nothing.
    """
    return ":".join(
        (
            "extract",
            str(file_version_id),
            extractor_id,
            extractor_version,
            model_version or "-",
            str(generation),
            str(input_asset_id) if input_asset_id is not None else "-",
        )
    )


async def _create_job(
    connection: AsyncConnection,
    *,
    extractor_id: str,
    manifest: Manifest,
    file_version_id: UUID,
    generation: int,
    input_asset_id: UUID | None = None,
    params: dict[str, Any] | None = None,
) -> Run:
    model = manifest.declared_model
    key = idempotency_key(
        file_version_id=file_version_id,
        extractor_id=extractor_id,
        extractor_version=manifest.version,
        model_version=model.version if model is not None else None,
        generation=generation,
        input_asset_id=input_asset_id,
    )
    queued = await operations.enqueue(
        connection,
        kind=kind_of(extractor_id),
        max_attempts=MAX_ATTEMPTS,
        priority=_priority(manifest),
        idempotency_key=key,
        payload={
            "file_version": str(file_version_id),
            "generation": generation,
            # Handed back on claim so an extractor can deduplicate its own side of an
            # at-least-once delivery without recomputing what the core keyed the job on.
            "idempotency_key": key,
            "params": dict(params or {}),
            "input_asset": str(input_asset_id) if input_asset_id is not None else None,
        },
        subject_type="file_version",
        subject_id=file_version_id,
    )

    # The run carries the job's id, so an extractor holding one holds the other. `DO NOTHING`
    # covers the case where `enqueue` converged on a job that already exists: then the run does
    # too, and this call is a no-op rather than a constraint violation.
    await connection.execute(
        insert(extraction_run)
        .values(
            id=queued.id,
            extractor_id=extractor_id,
            file_version_id=file_version_id,
            generation=generation,
            input_asset_id=input_asset_id,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    found = await get_run(connection, queued.id)
    if found is None:  # pragma: no cover - just inserted, in this transaction
        raise RuntimeError(f"extraction run {queued.id} vanished as it was created")
    return found


async def _reuse(
    connection: AsyncConnection,
    *,
    extractor_id: str,
    manifest: Manifest,
    file_version_id: UUID,
    content_hash: str,
    generation: int,
) -> Run | None:
    """Copy an earlier run's outputs instead of computing them again (F-009/FR-8).

    The condition is exact: the same extractor, the same implementation and model version, the
    same generation, over *byte-identical content*. Anything looser would be a different answer
    wearing this one's provenance.

    Nothing is copied on disk. A derived asset lives under the source content hash
    ([09 § storage](../../../specs/09-previews.md#storage)), and the hash is identical by
    definition here — so the bytes are already exactly where this version's assets belong. That
    is the layout paying for itself.
    """
    model = manifest.declared_model
    model_version = model.version if model is not None else None

    donor_row = (
        await connection.execute(
            select(extraction_run.c.id)
            .join(file_version, file_version.c.id == extraction_run.c.file_version_id)
            .where(
                extraction_run.c.extractor_id == extractor_id,
                extraction_run.c.state == "succeeded",
                extraction_run.c.generation == generation,
                extraction_run.c.extractor_version == manifest.version,
                extraction_run.c.model_version.is_not_distinct_from(model_version),
                extraction_run.c.input_asset_id.is_(None),
                extraction_run.c.file_version_id != file_version_id,
                file_version.c.content_hash == content_hash,
            )
            # The oldest: it is the one whose outputs have been available longest, and choosing
            # deterministically keeps two concurrent uploads of the same bytes from disagreeing.
            .order_by(extraction_run.c.created_at)
            .limit(1)
        )
    ).first()
    if donor_row is None:
        return None
    donor = donor_row[0]

    run_id = new_id()
    await connection.execute(
        insert(extraction_run).values(
            id=run_id,
            extractor_id=extractor_id,
            file_version_id=file_version_id,
            generation=generation,
            state="succeeded",
            extractor_version=manifest.version,
            model_version=model_version,
            reused_from=donor,
            started_at=func.now(),
            finished_at=func.now(),
        )
    )
    for table, columns in (
        (
            metadata_entry,
            (
                "key",
                "value_type",
                "provenance",
                "confidence",
                "value_text",
                "value_number",
                "value_boolean",
                "value_time",
                "value_latitude",
                "value_longitude",
                "value_json",
            ),
        ),
        (segment, ("ordinal", "text", "anchor_kind", "anchor", "confidence", "language")),
        # The claims come along too: identical bytes mean the same tags, and a duplicate file
        # that arrived with metadata but no tags would be a hole a reader cannot explain.
        (file_auto_tag, ("tag_id", "confidence")),
        (
            derived_asset,
            (
                "kind",
                "name",
                "media_type",
                "size_bytes",
                "content_hash",
                "digest_algorithm",
                "params",
                "rendition_kind",
            ),
        ),
    ):
        copied = (
            await connection.execute(
                select(*(table.c[name] for name in columns)).where(table.c.run_id == donor)
            )
        ).mappings()
        rows = [
            {
                **dict(row),
                "id": new_id(),
                "file_version_id": file_version_id,
                "run_id": run_id,
                "generation": generation,
            }
            for row in copied
        ]
        if rows:
            # One statement per table rather than per row: a long document is thousands of
            # segments, and reuse exists to be cheaper than the work it replaces.
            await connection.execute(insert(table), rows)

    # The copy is of somebody else's analysis, and this file may have refused some of it: a tag
    # the user rejected here must not arrive through the back door (F-003/FR-5).
    await tagging.drop_rejected_claims(connection, run_id=run_id)

    _logger.info("reused run %s for version %s (%s)", donor, file_version_id, extractor_id)
    found = await get_run(connection, run_id)
    if found is None:  # pragma: no cover - inserted in this transaction
        raise RuntimeError(f"reused run {run_id} vanished as it was created")
    return found


async def supersede_pending(connection: AsyncConnection, *, file_version_id: UUID) -> int:
    """Ask the extractors still working on this version to stop. Returns how many were asked.

    Called when a *newer* version of the same file arrives: whatever is still queued for the old
    one is work nobody is waiting for. Cooperative on purpose — cancellation reaches a running
    extractor through its next heartbeat (12 § leases & fencing), and a result that arrives
    anyway is still accepted, because the version it describes still exists and old versions
    stay searchable ([F-007](../../../features/F-007-versioning.md)).
    """
    pending = await connection.execute(
        select(extraction_run.c.id).where(
            extraction_run.c.file_version_id == file_version_id,
            extraction_run.c.state.notin_(TERMINAL_OPERATION_STATES),
        )
    )
    asked = 0
    for (run_id,) in pending.all():
        if await operations.request_cancel(connection, operation_id=run_id):
            asked += 1
    return asked


# ------------------------------------------------------------------------------------ status


async def get_run(connection: AsyncConnection, run_id: UUID) -> Run | None:
    result = await connection.execute(select(*_RUN_COLUMNS).where(extraction_run.c.id == run_id))
    row = result.first()
    return _as_run(tuple(row)) if row is not None else None


async def runs_for(connection: AsyncConnection, file_version_id: UUID) -> list[Run]:
    """Every run of one version, oldest first — the per-file extraction status (04 § status)."""
    result = await connection.execute(
        select(*_RUN_COLUMNS)
        .where(extraction_run.c.file_version_id == file_version_id)
        .order_by(extraction_run.c.created_at, extraction_run.c.extractor_id)
    )
    return [_as_run(tuple(row)) for row in result.all()]


def status_of(runs: Iterable[Run]) -> Status:
    """The coarse status a listing shows, derived rather than stored.

    Derived because it is a function of rows that already exist, and a stored copy would be one
    more thing to keep true across retries, supersessions and reprocessing. The five answers:

    - `none` — nothing runs on this file. Not a failure: no installed extractor accepts it, or
      none is installed at all;
    - `pending` — at least one job is queued or running. This is what a file looks like the
      moment it lands ([F-001/FR-8](../../../features/F-001-upload-and-import.md));
    - `indexed` — every job succeeded;
    - `partial` — everything finished, some succeeded, some did not;
    - `failed` — everything finished and none succeeded.
    """
    return status_from_states([run.state for run in runs])


def status_from_states(states: Sequence[str]) -> Status:
    """`status_of` over bare states, for the batched query that never builds the rows."""
    if not states:
        return "none"
    if any(state not in TERMINAL_OPERATION_STATES for state in states):
        return "pending"
    succeeded = sum(1 for state in states if state == "succeeded")
    if succeeded == len(states):
        return "indexed"
    return "partial" if succeeded else "failed"


async def statuses_of(
    connection: AsyncConnection, file_version_ids: Sequence[UUID]
) -> dict[UUID, Status]:
    """Statuses for a page of files, in one query rather than one per row."""
    if not file_version_ids:
        return {}

    result = await connection.execute(
        select(
            extraction_run.c.file_version_id,
            extraction_run.c.state,
            func.count().label("runs"),
        )
        .where(extraction_run.c.file_version_id.in_(file_version_ids))
        .group_by(extraction_run.c.file_version_id, extraction_run.c.state)
    )

    counted: dict[UUID, list[str]] = {}
    for version_id, state, runs in result.all():
        counted.setdefault(version_id, []).extend([state] * runs)

    # Every id asked about gets an answer, including the ones with no runs at all — a caller
    # rendering a listing must not have to tell "no extractor wanted it" from "absent from this
    # dictionary".
    return {
        version_id: status_from_states(counted.get(version_id, []))
        for version_id in file_version_ids
    }


async def counts_by_state(connection: AsyncConnection, *, extractor_id: str) -> dict[str, int]:
    """How much work one extractor has, by state — queue depth and failure rate (04 § status)."""
    result = await connection.execute(
        select(extraction_run.c.state, func.count())
        .where(extraction_run.c.extractor_id == extractor_id)
        .group_by(extraction_run.c.state)
    )
    return {str(state): int(count) for state, count in result.all()}


# ------------------------------------------------------------------------------- transitions


async def claim(
    connection: AsyncConnection,
    *,
    extractor_id: str,
    extractor_version: str | None,
    model_version: str | None,
    worker: str,
    lease: timedelta,
) -> ClaimedJob | None:
    """Claim one job for this extractor, or `None` if it has none due.

    The run is stamped with the versions of the extractor that *claimed* it, not the ones it was
    created with: an image upgraded between the two is a different program, and the provenance
    has to name the one that actually ran.
    """
    claimed = await operations.claim(
        connection, worker=worker, lease=lease, kinds=(kind_of(extractor_id),)
    )
    if claimed is None:
        # The idle path is where the mirror is caught up, for two reasons: a claim that found a
        # job's attempts spent has just dead-lettered it without any worker reporting that, and
        # an extractor asking for work is exactly when nobody is waiting on latency.
        await reconcile_runs(connection)
        return None

    run = await get_run(connection, claimed.id)
    version = await _version_facts(connection, run.file_version_id if run else None)
    if run is None or version is None:
        # Neither can happen through `route()`, which writes both rows in one transaction. If it
        # ever does, the job is unrunnable — so it fails without a retry rather than being
        # handed to an extractor that cannot be told what to look at.
        _logger.error("job %s has no run or no file version; failing it", claimed.id)
        await operations.fail(
            connection,
            claimed=claimed,
            error="the job has no run or no file version",
            retryable=False,
        )
        return None

    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(
            state="running",
            extractor_version=extractor_version,
            model_version=model_version,
            started_at=func.now(),
            updated_at=func.now(),
            error=None,
        )
    )
    started = await get_run(connection, claimed.id)
    if started is None:  # pragma: no cover - updated in this transaction
        raise RuntimeError(f"extraction run {claimed.id} vanished as it started")

    lease_expires_at = await _lease_expiry(connection, claimed.id)
    return ClaimedJob(
        operation=claimed,
        run=started,
        version=version,
        lease_expires_at=lease_expires_at,
        input=await asset_facts(connection, started.input_asset_id),
    )


async def asset_facts(connection: AsyncConnection, asset_id: UUID | None) -> AssetFacts | None:
    """One derived asset, with the version hash that says where its bytes are."""
    if asset_id is None:
        return None
    row = (
        await connection.execute(
            select(
                derived_asset.c.id,
                derived_asset.c.kind,
                derived_asset.c.name,
                derived_asset.c.media_type,
                derived_asset.c.size_bytes,
                derived_asset.c.content_hash,
                file_version.c.content_hash.label("source_hash"),
            )
            .join(file_version, file_version.c.id == derived_asset.c.file_version_id)
            .where(derived_asset.c.id == asset_id)
        )
    ).first()
    return None if row is None else AssetFacts(*tuple(row))  # pyright: ignore[reportArgumentType]


async def _version_facts(
    connection: AsyncConnection, file_version_id: UUID | None
) -> VersionFacts | None:
    if file_version_id is None:
        return None
    result = await connection.execute(
        select(*_VERSION_FACTS).where(file_version.c.id == file_version_id)
    )
    row = result.first()
    return VersionFacts(*tuple(row)) if row is not None else None  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True, slots=True)
class Lease:
    """Who holds a job and until when — the two columns `Operation` deliberately omits."""

    owner: str | None
    expires_at: datetime | None


async def lease_of(connection: AsyncConnection, operation_id: UUID) -> Lease:
    """The current lease, read from the row rather than remembered.

    The heartbeat needs the owner string the claim wrote, and the caller does not send it: the
    replica name in it is a diagnostic, while the thing that actually fences a write is the
    attempt (12 § leases & fencing). Reading it back is what lets both be true.
    """
    result = await connection.execute(
        select(operation.c.leased_by, operation.c.lease_expires_at).where(
            operation.c.id == operation_id
        )
    )
    row = result.first()
    return Lease(owner=None, expires_at=None) if row is None else Lease(row[0], row[1])


async def _lease_expiry(connection: AsyncConnection, operation_id: UUID) -> datetime:
    """Read back what the claim set, so the extractor is told the database's time, not ours."""
    expiry = (await lease_of(connection, operation_id)).expires_at
    if expiry is None:  # pragma: no cover - a claim always sets it
        raise RuntimeError(f"operation {operation_id} was claimed without a lease")
    return expiry


async def owned_job(
    connection: AsyncConnection, *, job_id: UUID, extractor_id: str
) -> tuple[Operation, Run] | None:
    """The job and its run, if they exist and belong to this extractor.

    The ownership check is the kind: a job for `pdf-text` is `extract.pdf-text`, so a credential
    bound to another extractor cannot heartbeat, feed or finish it.
    """
    found = await operations.get(connection, job_id)
    if found is None or found.kind != kind_of(extractor_id):
        return None
    run = await get_run(connection, job_id)
    return (found, run) if run is not None else None


async def finish(connection: AsyncConnection, *, claimed: Operation) -> bool:
    """Record a successful run. False when the lease was lost — the caller answers `409`."""
    if not await operations.succeed(connection, claimed=claimed):
        return False
    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(state="succeeded", finished_at=func.now(), updated_at=func.now(), error=None)
    )
    return True


async def complete(
    connection: AsyncConnection,
    *,
    claimed: Operation,
    envelope: results.Envelope,
    store: DerivedStore,
) -> results.Applied | None:
    """Apply one result and finish its job — the guarded transaction of 05 § dispatch.

    Fencing first, deliberately: a worker whose lease has gone should not have its outputs
    written and then discarded, and the answer it gets is the same either way. Then the rows,
    then routing again — because a result is how the *next* extractor's precondition becomes
    true, and doing it here means chaining needs no separate pass and cannot be forgotten.

    `None` means the claim was not current. Everything else raises, and the caller's transaction
    takes the whole envelope with it.
    """
    run = await get_run(connection, claimed.id)
    version = await _version_facts(connection, run.file_version_id if run else None)
    if run is None or version is None:  # pragma: no cover - both exist by construction
        raise RuntimeError(f"job {claimed.id} has no run or no file version")

    if not await finish(connection, claimed=claimed):
        return None

    applied = await results.apply(
        connection,
        run_id=run.id,
        file_id=version.file_id,
        file_version_id=run.file_version_id,
        source_hash=version.content_hash,
        generation=run.generation,
        envelope=envelope,
        store=store,
        operation_id=claimed.id,
    )
    await route(
        connection,
        file_version_id=run.file_version_id,
        media_type=version.media_type,
        content_hash=version.content_hash,
        generation=run.generation,
    )
    return applied


async def abandon(
    connection: AsyncConnection,
    *,
    claimed: Operation,
    error: str,
    retryable: bool,
    base_seconds: float,
    max_seconds: float,
) -> str:
    """Record a failed attempt, and mirror whatever the queue decided onto the run."""
    state = await operations.fail(
        connection,
        claimed=claimed,
        error=error,
        retryable=retryable,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
    )
    finished = None if state == "queued" else func.now()
    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(state=state, error=error, finished_at=finished, updated_at=func.now())
    )
    return state


async def release(connection: AsyncConnection, *, claimed: Operation, worker: str) -> bool:
    """Hand a claimed job back unstarted — a graceful stop, not a failure (12 § SIGTERM)."""
    if not await operations.release(connection, claimed=claimed, worker=worker):
        return False
    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(state="queued", started_at=None, updated_at=func.now())
    )
    return True


async def reconcile_runs(connection: AsyncConnection) -> int:
    """Mirror onto the runs whatever the queue decided without a worker there to say it.

    Two kinds of drift, both from a worker that stopped existing rather than stopping politely,
    and both invisible to the queue itself — its claim query treats an expired lease as
    claimable, which *is* the recovery story (ADR-0010). The record is what needs catching up,
    because the per-file status is read far more often than a job is lost:

    - **the job ended without its worker.** A claim that finds a job's attempts spent
      dead-letters it on the spot, so nobody ever reports that failure; a run left saying
      `running` would report a file as in progress forever.
    - **the job is waiting again.** The lease lapsed and the job is claimable, so `running` is
      no longer true of it.

    Returns how many runs it corrected. Idempotent, and safe to call from anywhere.
    """
    ended = await connection.execute(
        update(extraction_run)
        .where(
            extraction_run.c.state.notin_(TERMINAL_OPERATION_STATES),
            extraction_run.c.id == operation.c.id,
            operation.c.state.in_(TERMINAL_OPERATION_STATES),
        )
        .values(
            state=operation.c.state,
            error=func.coalesce(operation.c.error, extraction_run.c.error),
            finished_at=func.coalesce(extraction_run.c.finished_at, func.now()),
            updated_at=func.now(),
        )
    )

    waiting = await connection.execute(
        update(extraction_run)
        .where(
            extraction_run.c.state == "running",
            extraction_run.c.id == operation.c.id,
            or_(
                operation.c.state == "queued",
                and_(
                    operation.c.state == "running",
                    operation.c.lease_expires_at < func.now(),
                ),
            ),
        )
        .values(state="queued", started_at=None, updated_at=func.now())
    )
    return ended.rowcount + waiting.rowcount


__all__ = [
    "KIND_PREFIX",
    "MAX_ATTEMPTS",
    "AssetFacts",
    "ClaimedJob",
    "Lease",
    "Run",
    "Status",
    "VersionFacts",
    "abandon",
    "claim",
    "complete",
    "counts_by_state",
    "extractor_of",
    "finish",
    "get_run",
    "idempotency_key",
    "kind_of",
    "lease_of",
    "owned_job",
    "reconcile_runs",
    "release",
    "route",
    "runs_for",
    "status_of",
    "statuses_of",
    "supersede_pending",
]
