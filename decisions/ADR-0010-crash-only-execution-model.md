# ADR-0010 — Crash-only execution model

**Status:** Accepted
**Date:** 2026-07-31

## Context

A self-hosted single-server app has no HA story: upgrades, reboots, OOM kills, and power loss are routine, and "the app can be stopped and restarted anytime" is an explicit product requirement. At the same time the work is long and effectful: extraction jobs run minutes to hours ([04](../specs/04-ingestion-pipeline.md)), the initial import scans 10 TB ([F-001/FR-5](../features/F-001-upload-and-import.md)), and many operations mutate a filesystem that PostgreSQL transactions cannot cover (uploads, moves, trash safeguarding, purges, version snapshots, archive builds).

Graceful-shutdown handling alone cannot deliver this — SIGTERM never covers `kill -9` or power loss. A dedicated "startup recovery" routine would be the least-tested code in the system. And the existing specs require at-least-once delivery and idempotent result writes for *extraction jobs* ([04](../specs/04-ingestion-pipeline.md), [05](../specs/05-extractor-contract.md#job-lifecycle), [11](../specs/11-engineering-standards.md)) but neither generalize that to the other effectful operations nor address the database↔filesystem consistency gap.

## Decision

We adopt **crash-only design** (Candea & Fox, 2003): the only shutdown model is the crash. Every process must be killable at any instant without corruption, lost work, or duplicated effects. SIGTERM handling exists purely as an optimization — checkpoint, release leases, restart faster — never as a correctness mechanism.

Five rules bind all components ([12-reliability.md](../specs/12-reliability.md) is the normative mechanics):

1. **Durable intent first.** Any operation whose effects are not confined to a single PostgreSQL transaction exists as an operation record (a state-machine row) *before* its first side effect. No in-memory work state: schedules and timers are `next_due_at` columns; wake-up channels are lossy doorbells over durable state.
2. **Guarded transitions.** Every state transition is a compare-and-swap (`… WHERE state = $expected AND attempt = $token`); zero affected rows means superseded — discard your work. Events ride the same transaction ([ADR-0007](ADR-0007-unified-event-log.md)), making them exactly-once relative to state.
3. **Leases + fencing, not locks.** `SKIP LOCKED` is only the claim instant. Ownership is a heartbeat-extended lease; expired leases are re-claimable by anyone; attempts count **on claim** (poison jobs dead-letter even when they never report an error); every write-back carries the attempt/generation as a fencing token so a zombie holder is rejected.
4. **Idempotent effects by construction.** Database: natural unique keys, upserts, deterministic idempotency keys. Filesystem: staged write → fsync → atomic same-filesystem rename → directory fsync, deterministic paths, content-addressing where possible. Ordering rule: app-written bytes exist *before* the rows that reference them and are removed only *after* those rows are gone ([02 § invariant 8](../specs/02-domain-model.md#invariants)).
5. **Recovery is the normal path.** No boot-time recovery phase: the claim loop sweeps expired leases, the janitor collects debris continuously, and startup is just migrations plus starting the loops.

The binding, testable property: **after any prefix of any operation plus a restart, the system converges to the same terminal state — no debris past its grace window, no duplicated effects, no duplicated events.** Enforced by fault-injection tests and the `verify` audit ([11](../specs/11-engineering-standards.md#testing), [12](../specs/12-reliability.md#verification)).

## Consequences

- Stop/restart/upgrade anytime becomes a tested guarantee instead of a hope. Deploy overlap (old and new orchestrator briefly running together) is safe by the same lease mechanism, and moving workers to other hosts later is a configuration change.
- Recovery code is the normal code path, exercised every day — not a special branch that only runs after bad days.
- Discipline required: every new effectful operation ships as an operation record with guarded transitions and a row in the [operation inventory](../specs/12-reliability.md#operation-inventory); all file writes go through one shared write layer — ad-hoc `open()/write()` in feature code is a review-blocker.
- Crashes leak debris *by design* (staging files, unreferenced blobs); a janitor with grace windows is mandatory infrastructure, and staging areas must live on the destination filesystem — including inside the source tree (Q31).
- New obligations tracked as open questions: tuning defaults (Q30), staging placement (Q31), verifying rename/fsync semantics on NAS filesystems (Q32), operation-table hygiene (Q33), queue library vs. hand-rolled (Q34).
- Slight write amplification (intent rows, fsyncs) is accepted: for this product, correctness across restarts beats peak throughput.
- Explicitly not covered: disk death. Crash resistance is not backup (Q13).
