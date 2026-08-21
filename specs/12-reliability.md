# 12 — Reliability & Crash Safety

**Status:** Draft
**Related ADRs:** [ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md), [ADR-0001](../decisions/ADR-0001-postgresql-single-datastore.md), [ADR-0007](../decisions/ADR-0007-unified-event-log.md)

This spec is the normative mechanics for [ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md): the app is **crash-only** — any process may be killed at any instant (`kill -9`, OOM, power loss) without corruption, lost work, or duplicated effects. Graceful shutdown (SIGTERM) is an optimization that makes restart *fast*, never a mechanism that makes it *correct*.

**The binding property** (what fault-injection tests assert — [§ Verification](#verification)):

> After any prefix of any operation, plus a restart, the system converges to the same terminal state — no debris past its grace window, no duplicated effects, no duplicated events.

## What needs an operation record

The criterion is **not** duration. It is: *are the effects confined to a single PostgreSQL transaction?*

| Mutation | Treatment |
|---|---|
| Pure-DB, single transaction (add tag, create share, permission change) | Nothing extra — the transaction is already atomic, and its event rides in it ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)) |
| Everything else — touches the filesystem, spans transactions, calls an external service, or outlives the request | A durable **operation record**, created *before* the first side effect |

A rename is instant and still needs a record: it crosses the DB↔filesystem boundary. **In-memory work state is forbidden** — no in-process queues, no in-memory timers. Anything scheduled is a `next_due_at` column; a restart may lose only wall-clock time, never work.

## Operation records & guarded transitions

Every operation record is a state machine — `queued → running → succeeded | failed → queued (retry) | dead_letter`, plus `cancelled`/`superseded` where applicable ([05 § job lifecycle](05-extractor-contract.md#job-lifecycle) is the canonical instance).

- **Every transition is a compare-and-swap:** `UPDATE … SET state = 'succeeded' WHERE id = $1 AND state = 'running' AND attempt = $2`. Zero rows affected means someone else advanced or superseded the operation — the worker discards its work. This one discipline eliminates most double-execution bugs.
- **Events ride the transition's transaction** ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)). Because transitions are guarded, events are exactly-once *relative to state changes*: a job that runs twice emits its `succeeded` event once.
- **All time comparisons use database time** (`now()` in SQL). App-server clocks never participate in lease or schedule decisions.

## Leases & fencing

Row locks cannot own long work — a transaction held open for a 3-hour transcription dies on every deploy and bloats vacuum. So `FOR UPDATE SKIP LOCKED` is only the **claim instant**; ownership is a **lease**:

```sql
UPDATE operation SET
  state = 'running', leased_by = $worker,
  lease_expires_at = now() + $lease, attempt = attempt + 1
WHERE id = (
  SELECT id FROM operation
  WHERE state = 'queued'
     OR (state = 'running' AND lease_expires_at < now())  -- reclaim: recovery = the normal path
  ORDER BY priority, next_due_at
  FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

- **Heartbeats extend the lease** and double as the **cancellation channel**: the heartbeat response tells the worker to stop (user cancel, supersession by a newer generation). A heartbeat that comes back empty means the lease was lost — someone reclaimed the work — and the worker must abort immediately.
- **Attempts count on claim, not on failure.** A poison job that OOM-kills its worker never reports an error; counting on claim means the reclaim increments `attempt`, and `attempt > max_attempts` dead-letters the job anyway. This is what keeps one pathological file from eating a queue.
- **`attempt` is the fencing token.** Every write-back (result submission, heartbeat, transition) carries it and is CAS-guarded, so a zombie holder — a worker that outlived its lease — is rejected on write and its late result can never clobber a re-run. Reprocessing supersession fences the same way on `generation` ([F-009](../features/F-009-reprocessing.md)).
- **Idempotency keys are deterministic** — `hash(file_version, extractor id + version, model version, generation, params)` — so re-detecting the same work (a re-scan while the job is still pending) converges on the existing operation instead of duplicating it. The unique index covers **queued** rows, and convergence with a *running* operation is a check the enqueue performs rather than a constraint: a recurring operation queues its own successor while it is still executing, and a key held by the in-flight run would make that impossible. The residual race — a row transitioning `queued → running` between that check and the insert — yields one extra queued run, which is the at-least-once delivery the model already assumes and deduplicates on write ([05 § job lifecycle](05-extractor-contract.md#job-lifecycle)).
- Leases make **deploy overlap safe** — old and new orchestrator briefly both running simply compete for claims — and later multi-host workers a configuration change, not a redesign.

## Tuning defaults

Confirmed as the shipping defaults (Q30), every one env-tunable, with per-cost-class overrides where noted. They are conservative on purpose and get revisited at phase-2 entry against real extractor runtimes — a re-tune is a configuration change, never a design change.

| Knob | Default | Why this number |
|---|---|---|
| Lease duration | **5 min** | Long enough that a busy worker does not lose work to a missed heartbeat, short enough that a dead worker's job is reclaimed while someone still cares. Per-cost-class overrides. |
| Heartbeat cadence | **60 s** | Five chances to renew inside one lease, so a single slow cycle is survivable. Doubles as the cancellation channel. |
| `max_attempts` | **4** | Three retries past the first attempt before dead-lettering. Attempts count **on claim**, so a poison job that kills its worker still converges. |
| Retry backoff | **exponential with jitter** | Jitter is load-bearing: without it, a batch of failures retries in lockstep forever. |
| Janitor grace window | **24 h** | The window exists so the janitor cannot race an in-flight operation between its bytes-write and its row-commit; 24 h is what comparable products use for the same kind of temp-file TTL. |
| Upload-session expiry | **7 d** | An interrupted multi-GB upload survives a weekend; staging is not held indefinitely. Published to clients as `Upload-Limit: max-age` ([ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md)). |
| Workspace scan interval | **1 h** | The correctness backstop for external changes; the watcher and manual rescan are the fast paths ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)). Per-workspace tunable. |

## Filesystem write protocol

PostgreSQL transactions do not cover the NAS — this protocol closes the gap. Every file the app writes, everywhere (source tree, `versions/`, derived store), goes through **one shared write layer** implementing it; ad-hoc file IO in feature code is a review-blocker.

1. **Stage on the destination filesystem.** Write to a staging file named with the operation id, in a staging area that shares a filesystem with the destination — for workspace writes that is *inside the source tree* at `.workspace/staging/` ([03 § storage layout](03-storage-and-portability.md#storage-layout), [ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)); for `versions/` and the derived store, a staging directory on the same volume.
2. **`fsync` the file**, then **atomically `rename` onto the deterministic final path**, then **`fsync` the parent directory** — a rename is not durable until its directory entry is. Deterministic paths (derived from ids/content hashes, never minted fresh per attempt) make retries converge on the same target.
3. **Content-addressed areas are idempotent for free** (`versions/`, hashed derived assets): same bytes, same path — if the blob already exists, the write is a no-op.
4. **Cross-filesystem moves** (trash safeguarding and version snapshots when `{data-root}` and the app volume differ — [03](03-storage-and-portability.md#deletion--trash)) cannot be atomic; they run as a journaled sequence: copy → fsync → verify hash → commit reference → delete source. A crash between copy and delete leaves both copies — harmless, because the operation record says which step is next.

**Ordering rule ([02 § invariant 8](02-domain-model.md#invariants)): app-written bytes outlive the rows that reference them.**

- *Creating:* bytes first, row second. A crash orphans a file — harmless, janitor-collected. The reverse orphan (a committed row pointing at missing bytes) would be a user-visible 404 bug.
- *Deleting:* rows first, bytes second. Purge deletes domain rows and decrements blob refcounts in one transaction while recording unlink work items; unlinking runs deferred with retries (`unlink` is naturally idempotent — ENOENT means done). This also upholds [F-014/FR-9](../features/F-014-deletion-and-trash.md): purge only *frees* space, so a full disk can never block it.
- The one exception is external: content deleted directly on the NAS is reconciled by re-scan and flagged `restorable: false` ([F-014/FR-10](../features/F-014-deletion-and-trash.md)) — an accepted reality, never an app bug.

**The protocol's assumptions are checked, not assumed.** Rename atomicity and `fsync` honesty on SMB/NFS mounts differ from local POSIX in mount-option-dependent ways, and every guarantee here stands on them. So the `fs-check` probe exercises them against a workspace root before that workspace exists, refusing one whose filesystem fails, and v1 supports filesystems local to the app host ([03 § filesystem requirements](03-storage-and-portability.md#filesystem-requirements), [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)).

## Debris & the janitor

Every crash leaks *by design*: staging files, stale upload sessions, blobs whose referencing transaction never committed, half-built archives. Debris is safe because it is (a) **identifiable** — all temp writes carry their operation id and live in known staging areas — and (b) **collected** by the janitor, itself a periodic, leased, idempotent operation like any other:

- Staging entries whose operation is terminal (or unknown) and older than a **grace window** are deleted. The grace window exists so the janitor never races an in-flight operation between its bytes-write and its row-commit.
- Unreferenced content-addressed blobs older than the grace window are unlinked — but only against a **list of what is referenced**. Absence of that list means *skip*, never *collect*: `versions/` holds the only copy of every superseded version, so collecting against an empty list would empty it. Blob collection therefore stays inert until the feature that creates blob references registers one.
- Expired upload sessions are closed and their staging removed.
- [F-014](../features/F-014-deletion-and-trash.md)'s trash-retention janitor is the same machinery with a policy on top.

Grace window and upload-session expiry are in [§ tuning defaults](#tuning-defaults).

## Durable schedules, lossy doorbells

Generalizing [ADR-0007](../decisions/ADR-0007-unified-event-log.md)'s `LISTEN/NOTIFY` pattern: **every push channel is a lossy wake-up over durable state plus a periodic poll.** `NOTIFY` (job dispatch, event fan-out), filesystem watchers ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md) — a watcher event only ever *hastens* a scan the schedule would have run anyway), WebSocket pushes — all may drop; none may be load-bearing. The durable side is always a table: queued operations, `next_due_at` schedules (re-scan, janitor, retry backoff), the event log with consumer-held cursors. Server-side WebSocket state is deliberately none: thin notifications are lossy by design and clients resync via the `/events` cursor feed on reconnect ([F-012](../features/F-012-live-updates.md)).

## Client-visible idempotency

The *client's* connection crashes too: a response lost after commit leaves the caller not knowing whether the mutation happened. Unsafe `POST`s accept an **`Idempotency-Key`** ([08](08-api-principles.md#conventions-proposed)); the first execution's outcome (status + body) is recorded against the key and **replayed** on retry — a retry never re-executes. Keys are scoped per token, retained for a bounded window, and reuse the operation-record machinery (the header is simply the idempotency key of a client-initiated operation). This is what makes future sync clients safe to write.

## Job atomicity

**The operation is the atomic unit — no intra-job checkpointing.** A crash mid-transcription costs a re-run, never consistency ([04](04-ingestion-pipeline.md#stages)); large work gets resumability by *decomposition* (video → keyframes → per-keyframe jobs), which keeps the extractor contract simple. Results apply in one guarded transaction per job; large payloads submit two-phase — derived assets staged by content hash first, then one envelope referencing them ([05](05-extractor-contract.md#result-envelope); wire details Q5).

The import scan is the deliberate exception — one logical operation too big to be atomic — so it **checkpoints**: a durable cursor over a deterministic traversal order, committed in the same transaction as each registered batch (upserts by path + hash). Scans are *convergent, not snapshot-perfect*: whatever a concurrently-mutating tree hides from one pass, the next pass reconciles ([F-001/FR-5–6](../features/F-001-upload-and-import.md)).

## Queue hygiene

A 10 TB import creates millions of operation rows, and a hot claim query scanning a table full of `succeeded` rows degrades badly. Terminal rows (succeeded; dead-lettered once resolved) move to a history table or are pruned on a retention policy — the event log already keeps the permanent audit trail ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)). Claimable states get partial indexes. Which per-file/per-extractor status queries ([04 § status](04-ingestion-pipeline.md#status--observability-api-visible)) must stay answerable after pruning is Q33.

## Startup, deploys, shutdown

- **Startup is: run migrations, start the loops.** There is no recovery phase — the claim query's expired-lease branch *is* recovery, exercised every ordinary day, not only after crashes. The loops live in their own process (`store-everything worker`, the `orchestrator` service — [10 § topology](10-deployment-and-operations.md#topology)) so that background work cannot starve request handling; a worker's start-up also re-asserts the recurring schedules, which is idempotent. **A worker waits rather than failing when the schema is pending or the database is unreachable** — both are ordinary states on a fresh install, where the stack comes up before migrations are applied (Q20), and a crash there would only produce a restart loop while `/readyz` already reports the same fact.
- **Migrations** run before any worker touches the schema ([10 § upgrades](10-deployment-and-operations.md#upgrades--migrations), Q20): transactional where PostgreSQL allows, internally idempotent where not (`CREATE INDEX CONCURRENTLY`), serialized by a single-runner lock so racing containers don't double-apply.
- **Deploy overlap is safe** via leases ([above](#leases--fencing)); expand–contract migrations keep the old code working on the new schema for the overlap window.
- **SIGTERM = optimization:** stop claiming, checkpoint or abandon what's cheap, release held leases (a guarded transition back to `queued`) so a successor re-claims instantly instead of waiting out lease expiry. `stop_grace_period` is sized for this checkpoint-and-release — seconds, never job duration ([10](10-deployment-and-operations.md#crash-resistance-ops-view)).
- `/readyz` stays honest: database reachable, migrations current ([10](10-deployment-and-operations.md#health--readiness)).

## Verification

A guarantee that isn't tested doesn't exist ([11](11-engineering-standards.md#testing)):

- **Fault-injection tests** (the [11 § test layer](11-engineering-standards.md#testing) and per-FR verification method of the same name): a fault hook in the shared write layer kills the process at injected points around every filesystem mutation and every state transition; the harness restarts and asserts the binding property — terminal state reached, no debris past grace windows, no duplicated effects or events. This is [F-001](../features/F-001-upload-and-import.md)'s "kill the importer mid-run" acceptance criterion, generalized to every row of the inventory below.
- **`verify` — an fsck-style admin audit** (`store-everything verify`; an admin API surface follows with the admin UI), read-only and incremental-capable: every row-referenced blob exists (with hash spot-checks — the bit-rot check from [03 § integrity](03-storage-and-portability.md#integrity)); every unreferenced blob is younger than the grace window; version-blob refcounts add up; no operation sits non-terminal with a long-expired lease. It runs clean after every crash-injection test and is runnable on demand in production.
- **`fs-check` — the filesystem probe** (`store-everything fs-check <path>`; an admin API surface follows with workspace creation): exercises `fsync` on files *and* directories, rename onto an existing file, listing consistency, and that staging shares a device with its destination — the failures that actually occur on SMB and NFS mounts. It cannot prove atomicity, which would need a power cut observed from outside, and says so rather than implying otherwise. It also reports case-folding and Unicode-normalization behaviour as **facts**, since both change what "the same name" means ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)). It gates workspace creation and adoption, and is what makes this spec's assumptions a checked precondition rather than a hope ([03 § filesystem requirements](03-storage-and-portability.md#filesystem-requirements)).

## Operation inventory

The normative list of effectful operations and how each converges. A feature that adds an effectful operation adds a row here ([11 § docs first](11-engineering-standards.md#docs-first--read-before-update-after)).

| Operation | Intent record | Idempotence & recovery |
|---|---|---|
| Extraction job ([04](04-ingestion-pipeline.md), [05](05-extractor-contract.md#job-lifecycle)) | job row | lease + fencing; results applied in one guarded transaction keyed by idempotency key |
| Resumable upload ([F-001/FR-2](../features/F-001-upload-and-import.md), [ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md)) | upload session (`bytes_received`) | appends fsync'd, then offset committed; resume truncates staging to the committed offset; finalize = hash-verify → rename → FileVersion + extraction jobs in one transaction |
| Workspace create / adopt ([ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)) | workspace row created before the first directory is touched | deterministic paths (`data/`, `.workspace/`, `marker`); `mkdir` is idempotent, the marker is written by the staged-write protocol; a crash leaves a directory tree the retry adopts rather than duplicates. Refused outright when the `fs-check` probe fails |
| Import scan / re-scan ([F-001/FR-4–6](../features/F-001-upload-and-import.md)) | scan run + durable cursor | deterministic traversal; batch upserts by (path, hash) committed with the cursor; convergent across passes. Scheduled, manual, and watcher-triggered runs are the same operation — a watcher event only advances `next_due_at` ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)) |
| Move / rename / restore ([F-010/FR-1 contract](../features/F-010-auto-sort-inbox.md), [F-014/FR-4](../features/F-014-deletion-and-trash.md)) | move op (from → to) | same-fs rename is atomic; recovery inspects which side exists and rolls forward; cross-filesystem = journaled copy |
| Trash safeguarding ([F-014/FR-2](../features/F-014-deletion-and-trash.md)) | the `trashed` + not-yet-safeguarded state | content-addressed move into `versions/`; re-runs converge; out-of-space rolls back per FR-2 |
| Purge ([F-014/FR-7](../features/F-014-deletion-and-trash.md)) | purge op + unlink work items | rows + refcount decrements in one transaction; deferred unlinks retried (ENOENT = done); never blocked by a full disk |
| Version snapshot on app-mediated write ([03](03-storage-and-portability.md#versioning-vs-the-folder-is-everything-known-tension)) | write op | move-to-`versions/` then replace, each step recorded; cross-filesystem journaled |
| Archive build ([F-016](../features/F-016-archive-download.md)) | build job; manifest hash = idempotency key | staged write → rename; a lost build is a cache miss, rebuilt on demand |
| Reprocess run ([F-009](../features/F-009-reprocessing.md)) | run + generation | generation is the fencing token; per-file generation swap is one transaction (FR-4) |
| Event fan-out ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)) | the log itself | server holds no durable consumer state: `/events` clients own their cursors; WebSocket is lossy by design |
| Janitor runs ([above](#debris--the-janitor)) | janitor op (leased) | deletions idempotent; grace windows prevent racing in-flight operations; staging is collected only once its operation is terminal or gone |
| Recurring work (janitor, scheduled re-scan) | the pending operation row itself, keyed `schedule:{kind}` | a run queues its successor **in the transaction that completes it**, so the chain cannot break between the two; `ensure_scheduled` is the floor under it, safe to call on every start-up and the way a chain broken by a dead-letter is restored |
| Migrations ([10](10-deployment-and-operations.md#upgrades--migrations)) | migration ledger | transactional or internally idempotent; single-runner lock |

## Out of scope

- **Backup & restore** (Q13). Crash resistance survives *process* death, not *disk* death — leases don't replace `pg_dump`.
- **PostgreSQL's own durability** is configured, not designed, here: [10 § crash resistance](10-deployment-and-operations.md#crash-resistance-ops-view).
- **External workspace mirroring** (Q16) reuses this machinery when it is specified.
