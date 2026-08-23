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
- **A heartbeat that could not be *sent* is not a lost lease.** An empty response is a verdict — somebody else holds the row — while an unreachable database is only ignorance, and the two must not be confused. A failed renewal is retried for as long as the lease it renews could still be alive; the lease counts as lost when that window elapses, because from then on another claim may already own the work. The renewal loop is also the only channel that can stop a running job, so it may not end on a transient fault: a keeper that dies quietly leaves a handler running with nobody renewing its lease and no way to cancel it.
- **Only a cancellation somebody asked for is recorded as `cancelled`.** A worker cancelled from outside — a shutdown, a sibling task failing — stops its handler and hands the claim back (`running → queued`, guarded, no attempt consumed); a worker that lost its lease writes nothing and leaves the row to the reclaim branch. `cancelled` is terminal, so writing it on the way out of an unrelated failure would leave the work *less* recoverable than `kill -9` does, inverting [ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md)'s premise.
- **Attempts count on claim, not on failure.** A poison job that OOM-kills its worker never reports an error; counting on claim means the reclaim increments `attempt`, and `attempt > max_attempts` dead-letters the job anyway. This is what keeps one pathological file from eating a queue.
- **`attempt` is the fencing token.** Every write-back (result submission, heartbeat, transition) carries it and is CAS-guarded, so a zombie holder — a worker that outlived its lease — is rejected on write and its late result can never clobber a re-run. Reprocessing supersession fences the same way on `generation` ([F-009](../features/F-009-reprocessing.md)).
- **Idempotency keys are deterministic** — `hash(file_version, extractor id + version, model version, generation, params)` — so re-detecting the same work (a re-scan while the job is still pending) converges on the existing operation instead of duplicating it. The unique index covers **queued** rows, and convergence with a *running* operation is a check the enqueue performs rather than a constraint: a recurring operation queues its own successor while it is still executing, and a key held by the in-flight run would make that impossible. The residual race — a row transitioning `queued → running` between that check and the insert — yields one extra queued run, which is the at-least-once delivery the model already assumes and deduplicates on write ([05 § job lifecycle](05-extractor-contract.md#job-lifecycle)). The **same transition on the other side of the insert** is not allowed to cost anything at all: `ON CONFLICT DO NOTHING` locks no row, so the conflicting row can be claimed before the enqueue reads it back, and the read-back finding nothing must never surface as an error. An enqueue in the caller's transaction is running inside somebody's upload or scan batch, and a failure here would abort that; so the pair is retried, converging on the row that started running where the caller permits it and taking the key again where it does not.
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
| Watcher debounce window | **5 s** | How long a burst of filesystem events has to go quiet before it becomes one scan. A burst that never goes quiet is acted on every twelve windows anyway, so an import is visible while it runs rather than only when it stops — that multiple is a consequence of this number, not a second knob. |

## Filesystem write protocol

PostgreSQL transactions do not cover the NAS — this protocol closes the gap. Every file the app writes, everywhere (source tree, `versions/`, derived store), goes through **one shared write layer** implementing it; ad-hoc file IO in feature code is a review-blocker.

1. **Stage on the destination filesystem.** Write to a staging file named with the operation id, in a staging area that shares a filesystem with the destination — for workspace writes that is *inside the source tree* at `.workspace/staging/` ([03 § storage layout](03-storage-and-portability.md#storage-layout), [ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)); for `versions/` and the derived store, a staging directory on the same volume.
2. **`fsync` the file**, then **atomically `rename` onto the deterministic final path**, then **`fsync` the parent directory** — a rename is not durable until its directory entry is, and that holds for *every* directory the write had to create, not only the last one: a sharded path like `versions/ab/cd/` is two new entries, and syncing one of them leaves the other exactly as durable as the page cache. Deterministic paths (derived from ids/content hashes, never minted fresh per attempt) make retries converge on the same target.
3. **Content-addressed areas are idempotent for free** (`versions/`, hashed derived assets): same bytes, same path — if the blob already exists, the write is a no-op. It is not a *no-touch*, though: the existing blob's mtime is refreshed, because that timestamp is what the janitor reads as the age of its youngest reference ([§ debris & the janitor](#debris--the-janitor)).
4. **Cross-filesystem moves** (trash safeguarding and version snapshots when `{data-root}` and the app volume differ — [03](03-storage-and-portability.md#deletion--trash)) cannot be atomic; they run as a journaled sequence: copy → fsync → verify hash → commit reference → delete source. A crash between copy and delete leaves both copies — harmless, because the operation record says which step is next.

**Ordering rule ([02 § invariant 8](02-domain-model.md#invariants)): app-written bytes outlive the rows that reference them.**

- *Creating:* bytes first, row second. A crash orphans a file — harmless, janitor-collected. The reverse orphan (a committed row pointing at missing bytes) would be a user-visible 404 bug.
- *Deleting:* rows first, bytes second. Purge deletes domain rows and decrements blob refcounts in one transaction while recording unlink work items; unlinking runs deferred with retries (`unlink` is naturally idempotent — ENOENT means done). This also upholds [F-014/FR-9](../features/F-014-deletion-and-trash.md): purge only *frees* space, so a full disk can never block it.
- The one exception is external: content deleted directly on the NAS is reconciled by re-scan and flagged `restorable: false` ([F-014/FR-10](../features/F-014-deletion-and-trash.md)) — an accepted reality, never an app bug.

**The protocol's assumptions are checked, not assumed.** Rename atomicity and `fsync` honesty on SMB/NFS mounts differ from local POSIX in mount-option-dependent ways, and every guarantee here stands on them. So the `fs-check` probe exercises them against a workspace root before that workspace exists, refusing one whose filesystem fails, and v1 supports filesystems local to the app host ([03 § filesystem requirements](03-storage-and-portability.md#filesystem-requirements), [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)).

## Debris & the janitor

Every crash leaks *by design*: staging files, stale upload sessions, blobs whose referencing transaction never committed, half-built archives. Debris is safe because it is (a) **identifiable** — all temp writes carry their operation id and live in known staging areas — and (b) **collected** by the janitor, itself a periodic, leased, idempotent operation like any other:

- Staging entries whose operation is terminal (or unknown) and older than a **grace window** are deleted. The grace window exists so the janitor never races an in-flight operation between its bytes-write and its row-commit.
- Unreferenced content-addressed blobs older than the grace window are unlinked. The window means "recently written, so its row may still be committing", which is only true if every *reference* also touches the file: convergent content is stored once, so a new version can adopt a blob written months ago, and leaving its mtime alone would let a sweep whose reference snapshot predates that transaction collect the bytes a committed row points at. The write layer therefore freshens a blob it finds already stored, before the referencing row commits. Collection runs only against a **list of what is referenced**. Absence of that list means *skip*, never *collect*: `versions/` holds the only copy of every superseded version, so collecting against an empty list would empty it. Blob collection therefore stays inert until the feature that creates blob references registers one — the version write path, whose reference list is every digest a version still claims to be restorable from, **including current versions**: a snapshot taken before its version is superseded would otherwise be unreferenced for the length of an upload, and a long upload outlives the grace window.
- Expired upload sessions are closed and their staging removed.
- [F-014](../features/F-014-deletion-and-trash.md)'s trash-retention janitor is the same machinery with a policy on top.

Grace window and upload-session expiry are in [§ tuning defaults](#tuning-defaults).

## Folder rollups

Folder counts and sizes ([F-015/FR-8](../features/F-015-folders.md)) are maintained by a per-workspace `workspace.rollup` operation over a **delta queue**: every change inserts a row saying how much to add to its folder, on the same connection and in the same transaction as the change. This is the [ADR-0007](../decisions/ADR-0007-unified-event-log.md) outbox pattern applied to arithmetic rather than audit, and deliberately **not** a fourth consumer of the event log — a cursor over an append-only log can be overtaken by a transaction that commits behind the cursor's position, and a delta skipped that way is a number that stays wrong until something recomputes it.

Three properties make that queue cheap to trust:

- **Addition commutes**, so there is no ordering, no cursor and no exactly-once *delivery* requirement. The statement that claims a batch (`DELETE … RETURNING`) is the statement that applies it, so a crash re-applies nothing and loses nothing — the queue is simply still full. Batches commit individually, the scan's checkpoint pattern ([§ job atomicity](#job-atomicity)).
- **The closure does the fan-out.** A delta names one folder; joining the ancestor closure and grouping by ancestor turns a thousand uploads into one row written per folder in the chain. Nothing on the upload path updates an aggregate, which is what stops an import from serialising on the workspace root's row.
- **A rollup and a folder move are mutually exclusive per workspace** (`pg_advisory_xact_lock`), because a delta is expanded over the ancestors the closure holds *at drain time* and a move rewrites exactly those. The move reads the moved subtree's total from the stored aggregate, never from ground truth: ground truth would include changes still queued, and those land on the new chain by themselves.
- **A structural change carries the queue with it.** Two operations move a folder's rows out from under queued changes — a cross-workspace move, whose deltas are re-tagged to the destination workspace, and a folder identity transfer ([F-015/FR-7](../features/F-015-folders.md)), which hands the discarded row's deltas to the survivor before deleting it and compensates the two parent chains for the survivor's own. `folder_delta` cascades on folder deletion, so a row deleted with changes still queued against it takes them with it — a total short by an upload nobody can find again.

Drift is therefore possible only through a bug, and a rotating sweep riding each rollup run recomputes a small subset from ground truth, corrects what disagrees and logs it — skipping folders with a queued change beneath them, where a difference is lag. A workspace nothing has changed is armed by the janitor's pass, so quiet trees are verified too.

## Durable schedules, lossy doorbells

Generalizing [ADR-0007](../decisions/ADR-0007-unified-event-log.md)'s `LISTEN/NOTIFY` pattern: **every push channel is a lossy wake-up over durable state plus a periodic poll.** `NOTIFY` (job dispatch, event fan-out), filesystem watchers ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md) — a watcher event only ever *hastens* a scan the schedule would have run anyway), WebSocket pushes — all may drop; none may be load-bearing. The durable side is always a table: queued operations, `next_due_at` schedules (re-scan, janitor, retry backoff), the event log with consumer-held cursors. Server-side WebSocket state is deliberately none: thin notifications are lossy by design and clients resync via the `/events` cursor feed on reconnect ([F-012](../features/F-012-live-updates.md)).

## The request transaction

One request is one connection and therefore one transaction ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)), and it **commits when the handler returns, before the response starts**. That ordering is what makes a commit failure reportable: the request becomes a `5xx` instead of a `2xx` whose rows PostgreSQL threw away — "never `200` with an error body" ([08 § errors](08-api-principles.md#errors-rfc-9457)). It matters most where the transaction can still fail at `COMMIT` after the filesystem has already moved: the cross-workspace folder move defers its containment check to commit time, and the disk rename is not undone by a rollback.

The ordering is a property of the web framework's dependency lifecycle rather than of our own code, so it is **asserted by a test** (`server/tests/test_request_lifecycle.py`) instead of assumed from a version pin.

Two boundaries this does *not* move. A mutation whose effects reach outside that transaction needs an operation record ([§ what needs an operation record](#what-needs-an-operation-record)) — committing earlier says nothing about the filesystem. And a `2xx` means *committed*, not *received*: the response can still be lost on the way back, which is what the next section is for.

## Client-visible idempotency

The *client's* connection crashes too: a response lost after commit leaves the caller not knowing whether the mutation happened. Unsafe `POST`s accept an **`Idempotency-Key`** ([08](08-api-principles.md#conventions-proposed)); the first execution's outcome (status + body) is recorded against the key and **replayed** on retry — a retry never re-executes. Keys are scoped per token, retained for a bounded window, and reuse the operation-record machinery (the header is simply the idempotency key of a client-initiated operation). This is what makes future sync clients safe to write.

## Job atomicity

**The operation is the atomic unit — no intra-job checkpointing.** A crash mid-transcription costs a re-run, never consistency ([04](04-ingestion-pipeline.md#stages)); large work gets resumability by *decomposition* (video → keyframes → per-keyframe jobs), which keeps the extractor contract simple. Results apply in one guarded transaction per job; large payloads submit two-phase — derived assets staged by content hash first, then one envelope referencing them ([05](05-extractor-contract.md#result-envelope); wire details Q5).

The import scan is the deliberate exception — one logical operation too big to be atomic — so it **checkpoints**: a durable cursor over a deterministic traversal order, committed in the same transaction as each registered batch (upserts by path + hash). Scans are *convergent, not snapshot-perfect*: whatever a concurrently-mutating tree hides from one pass, the next pass reconciles ([F-001/FR-5–6](../features/F-001-upload-and-import.md)).

Concretely, the cursor is **a table of directories discovered and not yet processed**, and one batch is one directory: list it, register its files, insert its subdirectories, delete its own row — committed together. So a `kill -9` costs at most one directory's work, resuming needs no bookkeeping beyond that table, and a directory that *vanished* needs no special case at any depth: it stays on the frontier and popping it is simply an empty listing. Two facts about a directory are kept apart, because a later reconciliation acts on one and must never act on the other: **missing** (it is gone) and **unreadable** (we could not look), and only the first says anything about the files inside it.

## Queue hygiene

A 10 TB import creates millions of operation rows, and a hot claim query scanning a table full of `succeeded` rows degrades badly. Terminal rows (succeeded; dead-lettered once resolved) move to a history table or are pruned on a retention policy — the event log already keeps the permanent audit trail ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)). Claimable states get partial indexes. Which per-file/per-extractor status queries ([04 § status](04-ingestion-pipeline.md#status--observability-api-visible)) must stay answerable after pruning is Q33.

## Startup, deploys, shutdown

- **Startup is: run migrations, start the loops.** There is no recovery phase — the claim query's expired-lease branch *is* recovery, exercised every ordinary day, not only after crashes. The loops live in their own process (`store-everything worker`, the `orchestrator` service — [10 § topology](10-deployment-and-operations.md#topology)) so that background work cannot starve request handling; a worker's start-up also re-asserts the recurring schedules, which is idempotent. **A worker waits rather than failing when the schema is pending or the database is unreachable** — both are ordinary states on a fresh install, where the stack comes up before migrations are applied (Q20), and a crash there would only produce a restart loop while `/readyz` already reports the same fact. The same holds once it is *running*: a claim that fails is backed off and retried rather than ending its loop, because the loops are siblings in one task group and the first to raise would otherwise stop the whole process from claiming anything.
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
| Resumable upload ([F-001/FR-2](../features/F-001-upload-and-import.md), [ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md)) | upload session (`bytes_received`) | appends fsync'd, then offset committed; resume truncates staging to the committed offset, and staging that no longer covers that offset ends the session instead of appending at the wrong position; finalize = hash-verify → rename → FileVersion + extraction jobs in one transaction, **serialised per destination path** (`pg_advisory_xact_lock`) because the sequence is a check-then-act across the database and the filesystem |
| Workspace create / adopt ([ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)) | workspace row created before the first directory is touched | deterministic paths (`data/`, `.workspace/`, `marker`); `mkdir` is idempotent, the marker is written by the staged-write protocol; a crash leaves a directory tree the retry adopts rather than duplicates. Refused outright when the `fs-check` probe fails |
| Import scan / re-scan ([F-001/FR-4–6](../features/F-001-upload-and-import.md)) | scan run + frontier of pending directories | one directory per committed batch; the run is keyed by its operation, so a re-claim resumes *that* run rather than starting a second; one running scan per workspace, and a run left behind by a dead-lettered operation is reconciled on the next start rather than blocking every future scan. Scheduled, manual, and watcher-triggered runs are the same operation — a manual re-scan and (later) a watcher event only advance `next_due_at` ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)). **Reconciling what the pass did not see** runs only once the frontier is empty — "did not see" means nothing before that — in committed batches, and needs no cursor of its own: the query asks for live files the run did not stamp, and every row it handles stops matching it. **Folder identity** ([F-015/FR-7](../features/F-015-folders.md)) runs last and in one transaction, on evidence that belongs to the run: a re-claimed operation resumes its own run, so a crash before that commit costs the pass nothing and it decides the same way again |
| Move / rename / restore ([F-010/FR-1 contract](../features/F-010-auto-sort-inbox.md), [F-014/FR-4](../features/F-014-deletion-and-trash.md)) | move op (from → to) | same-fs rename is atomic; recovery inspects which side exists and rolls forward; cross-filesystem = journaled copy |
| Trash safeguarding ([F-014/FR-2](../features/F-014-deletion-and-trash.md)) | the `trashed` + not-yet-safeguarded state | content-addressed move into `versions/`; re-runs converge; out-of-space rolls back per FR-2 |
| Purge ([F-014/FR-7](../features/F-014-deletion-and-trash.md)) | purge op + unlink work items | rows + refcount decrements in one transaction; deferred unlinks retried (ENOENT = done); never blocked by a full disk |
| Version snapshot on app-mediated write ([03](03-storage-and-portability.md#versioning-vs-the-folder-is-everything-known-tension)) | write op | move-to-`versions/` then replace, each step recorded; cross-filesystem journaled |
| Archive build ([F-016](../features/F-016-archive-download.md)) | build job; manifest hash = idempotency key | staged write → rename; a lost build is a cache miss, rebuilt on demand |
| Reprocess run ([F-009](../features/F-009-reprocessing.md)) | run + generation | generation is the fencing token; per-file generation swap is one transaction (FR-4) |
| Event fan-out ([ADR-0007](../decisions/ADR-0007-unified-event-log.md)) | the log itself | server holds no durable consumer state: `/events` clients own their cursors; WebSocket is lossy by design |
| Janitor runs ([above](#debris--the-janitor)) | janitor op (leased) | deletions idempotent; grace windows prevent racing in-flight operations; staging is collected only once its operation is terminal or gone |
| Folder rollups ([above](#folder-rollups)) | the delta queue itself, written with the change it describes | claim and apply are one statement, so a crash leaves the queue intact rather than half-applied; addition commutes, so no ordering is needed; a rollup and a folder move in one workspace exclude each other, so no delta is expanded over a tree that has since changed; a rotating ground-truth sweep corrects what a bug got wrong |
| Recurring work (janitor, scheduled re-scan) | the pending operation row itself, keyed `schedule:{kind}` — plus the subject where the cadence is per-object rather than per-instance (`schedule:workspace.scan:{workspace}`, since every workspace carries its own interval), and a narrower scope where the work is a slice of that subject (a subtree re-scan) | a run queues its successor **in the transaction that completes it**, so the chain cannot break between the two; `ensure_scheduled` is the floor under it, safe to call on every start-up and the way a chain broken by a dead-letter is restored |
| Migrations ([10](10-deployment-and-operations.md#upgrades--migrations)) | migration ledger | transactional or internally idempotent; single-runner lock |

## Out of scope

- **Backup & restore** (Q13). Crash resistance survives *process* death, not *disk* death — leases don't replace `pg_dump`.
- **PostgreSQL's own durability** is configured, not designed, here: [10 § crash resistance](10-deployment-and-operations.md#crash-resistance-ops-view).
- **External workspace mirroring** (Q16) reuses this machinery when it is specified.
