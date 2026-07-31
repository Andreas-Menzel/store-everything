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
- **Idempotency keys are deterministic** — `hash(file_version, extractor id + version, model version, generation, params)` — with a unique index over non-terminal states, so re-detecting the same work (a re-scan while the job is still pending) converges on the existing operation instead of duplicating it.
- Leases make **deploy overlap safe** — old and new orchestrator briefly both running simply compete for claims — and later multi-host workers a configuration change, not a redesign.

Proposed defaults (confirm: Q30): lease 5 min, heartbeat every 60 s, `max_attempts` 4, exponential backoff with jitter between attempts; per-cost-class overrides.

## Filesystem write protocol

PostgreSQL transactions do not cover the NAS — this protocol closes the gap. Every file the app writes, everywhere (source tree, `versions/`, derived store), goes through **one shared write layer** implementing it; ad-hoc file IO in feature code is a review-blocker.

1. **Stage on the destination filesystem.** Write to a staging file named with the operation id, in a staging area that shares a filesystem with the destination — for workspace writes that is *inside the source tree* at `.workspace/staging/` ([03 § storage layout](03-storage-and-portability.md#storage-layout-proposed), Q31); for `versions/` and the derived store, a staging directory on the same volume.
2. **`fsync` the file**, then **atomically `rename` onto the deterministic final path**, then **`fsync` the parent directory** — a rename is not durable until its directory entry is. Deterministic paths (derived from ids/content hashes, never minted fresh per attempt) make retries converge on the same target.
3. **Content-addressed areas are idempotent for free** (`versions/`, hashed derived assets): same bytes, same path — if the blob already exists, the write is a no-op.
4. **Cross-filesystem moves** (trash safeguarding and version snapshots when `{data-root}` and the app volume differ — [03](03-storage-and-portability.md#deletion--trash)) cannot be atomic; they run as a journaled sequence: copy → fsync → verify hash → commit reference → delete source. A crash between copy and delete leaves both copies — harmless, because the operation record says which step is next.

**Ordering rule ([02 § invariant 8](02-domain-model.md#invariants)): app-written bytes outlive the rows that reference them.**

- *Creating:* bytes first, row second. A crash orphans a file — harmless, janitor-collected. The reverse orphan (a committed row pointing at missing bytes) would be a user-visible 404 bug.
- *Deleting:* rows first, bytes second. Purge deletes domain rows and decrements blob refcounts in one transaction while recording unlink work items; unlinking runs deferred with retries (`unlink` is naturally idempotent — ENOENT means done). This also upholds [F-014/FR-9](../features/F-014-deletion-and-trash.md): purge only *frees* space, so a full disk can never block it.
- The one exception is external: content deleted directly on the NAS is reconciled by re-scan and flagged `restorable: false` ([F-014/FR-10](../features/F-014-deletion-and-trash.md)) — an accepted reality, never an app bug.

**NAS caveat:** rename atomicity and fsync honesty on SMB/NFS mounts differ from local POSIX in mount-option-dependent ways. The whole protocol leans on them, so they are verified on the target hardware before v1 (Q32).

## Debris & the janitor

Every crash leaks *by design*: staging files, stale upload sessions, blobs whose referencing transaction never committed, half-built archives. Debris is safe because it is (a) **identifiable** — all temp writes carry their operation id and live in known staging areas — and (b) **collected** by the janitor, itself a periodic, leased, idempotent operation like any other:

- Staging entries whose operation is terminal (or unknown) and older than a **grace window** are deleted. The grace window exists so the janitor never races an in-flight operation between its bytes-write and its row-commit.
- Unreferenced content-addressed blobs older than the grace window are unlinked.
- Expired upload sessions are closed and their staging removed.
- [F-014](../features/F-014-deletion-and-trash.md)'s trash-retention janitor is the same machinery with a policy on top.

Proposed defaults (Q30): grace window 24 h, upload-session expiry 7 days.

## Durable schedules, lossy doorbells

Generalizing [ADR-0007](../decisions/ADR-0007-unified-event-log.md)'s `LISTEN/NOTIFY` pattern: **every push channel is a lossy wake-up over durable state plus a periodic poll.** `NOTIFY` (job dispatch, event fan-out), inotify watchers (Q3), WebSocket pushes — all may drop; none may be load-bearing. The durable side is always a table: queued operations, `next_due_at` schedules (re-scan, janitor, retry backoff), the event log with consumer-held cursors. Server-side WebSocket state is deliberately none: thin notifications are lossy by design and clients resync via the `/events` cursor feed on reconnect ([F-012](../features/F-012-live-updates.md)).

## Client-visible idempotency

The *client's* connection crashes too: a response lost after commit leaves the caller not knowing whether the mutation happened. Unsafe `POST`s accept an **`Idempotency-Key`** ([08](08-api-principles.md#conventions-proposed)); the first execution's outcome (status + body) is recorded against the key and **replayed** on retry — a retry never re-executes. Keys are scoped per token, retained for a bounded window, and reuse the operation-record machinery (the header is simply the idempotency key of a client-initiated operation). This is what makes future sync clients safe to write.

## Job atomicity

**The operation is the atomic unit — no intra-job checkpointing.** A crash mid-transcription costs a re-run, never consistency ([04](04-ingestion-pipeline.md#stages)); large work gets resumability by *decomposition* (video → keyframes → per-keyframe jobs), which keeps the extractor contract simple. Results apply in one guarded transaction per job; large payloads submit two-phase — derived assets staged by content hash first, then one envelope referencing them ([05](05-extractor-contract.md#result-envelope); wire details Q5).

The import scan is the deliberate exception — one logical operation too big to be atomic — so it **checkpoints**: a durable cursor over a deterministic traversal order, committed in the same transaction as each registered batch (upserts by path + hash). Scans are *convergent, not snapshot-perfect*: whatever a concurrently-mutating tree hides from one pass, the next pass reconciles ([F-001/FR-5–6](../features/F-001-upload-and-import.md)).

## Queue hygiene

A 10 TB import creates millions of operation rows, and a hot claim query scanning a table full of `succeeded` rows degrades badly. Terminal rows (succeeded; dead-lettered once resolved) move to a history table or are pruned on a retention policy — the event log already keeps the permanent audit trail ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)). Claimable states get partial indexes. Which per-file/per-extractor status queries ([04 § status](04-ingestion-pipeline.md#status--observability-api-visible)) must stay answerable after pruning is Q33.

## Startup, deploys, shutdown

- **Startup is: run migrations, start the loops.** There is no recovery phase — the claim query's expired-lease branch *is* recovery, exercised every ordinary day, not only after crashes.
- **Migrations** run before any worker touches the schema ([10 § upgrades](10-deployment-and-operations.md#upgrades--migrations), Q20): transactional where PostgreSQL allows, internally idempotent where not (`CREATE INDEX CONCURRENTLY`), serialized by a single-runner lock so racing containers don't double-apply.
- **Deploy overlap is safe** via leases ([above](#leases--fencing)); expand–contract migrations keep the old code working on the new schema for the overlap window.
- **SIGTERM = optimization:** stop claiming, checkpoint or abandon what's cheap, release held leases (a guarded transition back to `queued`) so a successor re-claims instantly instead of waiting out lease expiry. `stop_grace_period` is sized for this checkpoint-and-release — seconds, never job duration ([10](10-deployment-and-operations.md#crash-resistance-ops-view)).
- `/readyz` stays honest: database reachable, migrations current ([10](10-deployment-and-operations.md#health--readiness)).

## Verification

A guarantee that isn't tested doesn't exist ([11](11-engineering-standards.md#testing)):

- **Fault-injection tests** (the [11 § test layer](11-engineering-standards.md#testing) and per-FR verification method of the same name): a fault hook in the shared write layer kills the process at injected points around every filesystem mutation and every state transition; the harness restarts and asserts the binding property — terminal state reached, no debris past grace windows, no duplicated effects or events. This is [F-001](../features/F-001-upload-and-import.md)'s "kill the importer mid-run" acceptance criterion, generalized to every row of the inventory below.
- **`verify` — an fsck-style admin audit** (CLI + admin API), read-only and incremental-capable: every row-referenced blob exists (with hash spot-checks — the bit-rot check from [03 § integrity](03-storage-and-portability.md#integrity)); every unreferenced blob is younger than the grace window; version-blob refcounts add up; no operation sits non-terminal with a long-expired lease. It runs clean after every crash-injection test and is runnable on demand in production.

## Operation inventory

The normative list of effectful operations and how each converges. A feature that adds an effectful operation adds a row here ([11 § docs first](11-engineering-standards.md#docs-first--read-before-update-after)).

| Operation | Intent record | Idempotence & recovery |
|---|---|---|
| Extraction job ([04](04-ingestion-pipeline.md), [05](05-extractor-contract.md#job-lifecycle)) | job row | lease + fencing; results applied in one guarded transaction keyed by idempotency key |
| Chunked upload ([F-001/FR-2](../features/F-001-upload-and-import.md)) | upload session (`bytes_received`) | chunks fsync'd, then offset committed; resume truncates staging to the recorded offset; finalize = hash-verify → rename → FileVersion + extraction jobs in one transaction |
| Import scan / re-scan ([F-001/FR-4–6](../features/F-001-upload-and-import.md)) | scan run + durable cursor | deterministic traversal; batch upserts by (path, hash) committed with the cursor; convergent across passes |
| Move / rename / restore ([F-010/FR-1 contract](../features/F-010-auto-sort-inbox.md), [F-014/FR-4](../features/F-014-deletion-and-trash.md)) | move op (from → to) | same-fs rename is atomic; recovery inspects which side exists and rolls forward; cross-filesystem = journaled copy |
| Trash safeguarding ([F-014/FR-2](../features/F-014-deletion-and-trash.md)) | the `trashed` + not-yet-safeguarded state | content-addressed move into `versions/`; re-runs converge; out-of-space rolls back per FR-2 |
| Purge ([F-014/FR-7](../features/F-014-deletion-and-trash.md)) | purge op + unlink work items | rows + refcount decrements in one transaction; deferred unlinks retried (ENOENT = done); never blocked by a full disk |
| Version snapshot on app-mediated write ([03](03-storage-and-portability.md#versioning-vs-the-folder-is-everything-known-tension)) | write op | move-to-`versions/` then replace, each step recorded; cross-filesystem journaled |
| Archive build ([F-016](../features/F-016-archive-download.md)) | build job; manifest hash = idempotency key | staged write → rename; a lost build is a cache miss, rebuilt on demand |
| Reprocess run ([F-009](../features/F-009-reprocessing.md)) | run + generation | generation is the fencing token; per-file generation swap is one transaction (FR-4) |
| Event fan-out ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)) | the log itself | server holds no durable consumer state: `/events` clients own their cursors; WebSocket is lossy by design |
| Janitor runs ([above](#debris--the-janitor)) | janitor op (leased) | deletions idempotent; grace windows prevent racing in-flight operations |
| Migrations ([10](10-deployment-and-operations.md#upgrades--migrations)) | migration ledger | transactional or internally idempotent; single-runner lock |

## Out of scope

- **Backup & restore** (Q13). Crash resistance survives *process* death, not *disk* death — leases don't replace `pg_dump`.
- **PostgreSQL's own durability** is configured, not designed, here: [10 § crash resistance](10-deployment-and-operations.md#crash-resistance-ops-view).
- **External workspace mirroring** (Q16) reuses this machinery when it is specified.
