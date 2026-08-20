# ADR-0013 — The operation layer is ours: no job-queue library

**Status:** Accepted
**Date:** 2026-08-20

## Context

[12-reliability](../specs/12-reliability.md) is the normative mechanics of [ADR-0010](ADR-0010-crash-only-execution-model.md), and it asks for more than a job queue. Its operation record must provide: a durable intent row written before the first side effect for **every** effectful operation (extraction jobs, chunked uploads, import scans, moves, trash safeguarding, purges, version snapshots, archive builds, reprocess runs, janitor sweeps, migrations); guarded compare-and-swap transitions; `FOR UPDATE SKIP LOCKED` claiming with heartbeat-extended leases; **the heartbeat doubling as the cancellation channel**; `attempt` as a **fencing token** carried on every write-back so a zombie worker's late result is rejected; attempts counted on claim, not on failure; deterministic idempotency keys with a unique index over non-terminal states; priority classes; per-class and per-extractor concurrency caps with runtime pause/resume ([Q17](../OPEN-QUESTIONS.md)); retries with backoff into dead-letter; and terminal-row retention ([Q33](../OPEN-QUESTIONS.md)).

[Q34](../OPEN-QUESTIONS.md) asked whether a mature PostgreSQL-native queue library satisfies that, or whether we own the layer. A survey of the field (River, Oban, procrastinate, pgqueuer, chancy, pg-boss, graphile-worker, hatchet, apalis, JobRunr) produced three findings:

1. **No library ships fencing tokens.** In every ecosystem, rejecting a stale worker's late writes is something the application builds on top of the job row's attempt counter. The single property that makes zombie workers harmless is ours to implement regardless of what we adopt.
2. **The strongest Python candidate still leaves gaps.** procrastinate is mature and has transactional defer, 10-second heartbeats with stalled-job detection, priorities, dedup locks, cron, and pruning — but no runtime queue pause, no fencing, and its stalled-job retry is a recipe to wire rather than a default. pgqueuer is technically close but solo-maintained; hatchet requires running a separate engine and enqueuing over gRPC, forfeiting same-transaction enqueue — the one property we cannot trade.
3. **The operation record is not only a queue.** It is also the idempotency-key store that replays a lost response ([12 § client-visible idempotency](../specs/12-reliability.md#client-visible-idempotency)), the backing of the job-status API (`GET /jobs/{id}`, per-file and per-extractor progress — [08](../specs/08-api-principles.md), [04 § status](../specs/04-ingestion-pipeline.md#status--observability-api-visible)), and the anchor from which audit events are emitted in the same transaction ([ADR-0007](ADR-0007-unified-event-log.md)). A library owning a private schema would force a shadow table beside it, and two tables that must agree is the class of bug this project's architecture exists to avoid ([ADR-0001](ADR-0001-postgresql-single-datastore.md)).

Two cautionary data points from Immich, the closest comparable product, were weighed: a community attempt to replace its Redis-backed queue with a PostgreSQL one was abandoned unmerged, and its socket.io PostgreSQL adapter was reverted because `NOTIFY` fired every five seconds inside transactions, writing WAL continuously so the disk never idled. The second is a design pitfall with a name, not a verdict on PostgreSQL queues — River and Oban run this pattern in production at far larger scale.

## Decision

We will **own the operation layer** — one table and one shared module implementing [12](../specs/12-reliability.md) — and take **no job-queue library** as a dependency.

- **One layer for every effectful operation**, not a queue for extraction plus ad-hoc handling elsewhere. Every row of the [operation inventory](../specs/12-reliability.md#operation-inventory) runs through it; a feature that invents its own job handling is a review-blocker, exactly as ad-hoc file IO is.
- **The SQL is written by hand** ([ADR-0012](ADR-0012-python-fastapi-core-stack.md)) — the claim, transition, heartbeat, and reclaim statements are already spelled out in [12 § leases & fencing](../specs/12-reliability.md#leases--fencing).
- **`LISTEN/NOTIFY` is emitted only on real state changes, never on a timer**, and every consumer additionally polls its durable `next_due_at` ([12 § durable schedules, lossy doorbells](../specs/12-reliability.md#durable-schedules-lossy-doorbells)). An idle instance performs no writes: doorbells are an optimization over polling, and this rule is what keeps the failure Immich hit out of our design.
- **Verification is not optional.** The layer ships with the fault-injection harness as its primary test surface ([11](../specs/11-engineering-standards.md#testing)), property-based tests over the state machine, and the `verify` audit run after every crash-injection test.

## Consequences

- **We own the correctness of a component that is classically easy to get wrong.** That is the real cost, and it is paid deliberately: the mechanics are already specified statement by statement, fault-injection is a blocking CI gate from the first operation that exists, and attempts-on-claim plus CAS transitions remove the two most common double-execution bugs by construction.
- **No library semantics to fight and no upgrade treadmill.** We do not inherit timeout-based rescue where we specified heartbeats, or work around a missing pause because an upstream feature request is open.
- **One queueing model, one status surface, one retention policy** — the job-status API, idempotency replay, and audit linkage read the same rows the workers claim.
- **More code in phase 1** than importing a library would have cost. Accepted in exchange for fencing, runtime pause, and the operation record being a product surface rather than an implementation detail.
- **The layer stays behind one module boundary**, so its internals remain replaceable — but any replacement inherits the same obligations (fencing, cancellation-by-heartbeat, status queryability), which is what disqualified the libraries in the first place.
- Resolves [Q34](../OPEN-QUESTIONS.md); [Q17](../OPEN-QUESTIONS.md) (scheduling surface), [Q30](../OPEN-QUESTIONS.md) (tuning defaults), and [Q33](../OPEN-QUESTIONS.md) (table hygiene) remain open and are now unambiguously ours to answer.
