# ADR-0020 — Extractor dispatch: poll-based workers over an extractor-facing HTTP API

**Status:** Accepted
**Date:** 2026-08-23

## Context

[ADR-0002](ADR-0002-extractor-containers-fixed-api.md) fixed *what* an extractor is — a separate container speaking one versioned contract, pull-based file access, async job lifecycle, manifest-driven routing. It deliberately left *how* jobs move (Q5): push (core calls an extractor endpoint) versus poll (extractor pulls from the queue), the exact wire format, and how chaining declares its inputs.

Two facts decide this. First, we own the queue: [ADR-0013](ADR-0013-owned-operation-layer.md)'s operation layer already provides claims with leases, heartbeat-as-cancellation, attempt-as-fencing-token, priorities, idempotency keys, and dead-lettering — [12](../specs/12-reliability.md#leases--fencing) noted at the time that poll "composes more naturally" with that model. Second, the survey of comparable systems (2026-08): every self-hosted app that crosses a container boundary for analysis runs the analyzer as a **long-running service** (Immich's ML sidecar, Paperless-ngx's Tika/Gotenberg, Frigate's network detectors); none spawns per-file containers. The one shipping third-party-container contract, Nextcloud's ExApps, authenticates with per-app shared secrets — and the current hardening pattern (n8n task runners, Nextcloud HaRP) has the sandboxed side *dial out*, so it needs no inbound exposure at all.

A push design would force the inverse of all of this: extractors running HTTP listeners (an attack surface inside the sandbox), the core managing delivery state per extractor, and hours-long jobs needing either long-lived requests or extractor-side job stores.

## Decision

**Dispatch is poll.** An extractor is a long-running worker that claims its own jobs from the core; the core never initiates a connection to an extractor.

1. **Extractor-facing HTTP API** at `/extractor-api/v1/*`, served by the core API service, with its **own OpenAPI document** (it is a separate contract with separate versioning — `extractor-api/v1` — and a separate audience: extractor authors and the conformance kit). It never appears in the user-facing schema.
2. **Jobs are operation rows**, kind `extract.{extractor-id}` — the extraction queue *is* the operation layer: same claim query, leases, fencing, priorities ([04 § prioritization](../specs/04-ingestion-pipeline.md#prioritization--scheduling)), retry/backoff, dead-letter events. The in-process runner never claims `extract.*` kinds (it only claims kinds it has handlers for); the extractor is the worker.
3. **`ExtractionRun` is the durable provenance anchor**, created 1:1 with the job (same id). Every derived row references the run ([02 § invariants](../specs/02-domain-model.md#invariants) #3); the run outlives queue-hygiene pruning of terminal operation rows (Q33).
4. **Wire protocol** (normative detail in [05 § dispatch & wire protocol](../specs/05-extractor-contract.md#dispatch--wire-protocol-extractor-apiv1)): `PUT /registration` (manifest upsert on startup) · `POST /jobs/claim` (long-poll; returns the job payload with the claim's **attempt** as fencing token) · `POST /jobs/{id}/heartbeat` (extends the lease and returns the cancellation verdict; required at least every half lease) · `GET /jobs/{id}/inputs/{n}` (Range-capable byte streams) · `PUT /jobs/{id}/assets/{sha256}` (two-phase result staging, hash-verified, idempotent) · `POST /jobs/{id}/result` (one envelope, applied in one guarded transaction) · `POST /jobs/{id}/error`. Every write-back carries the attempt; a submission from a lost lease or superseded generation is rejected — exactly [12](../specs/12-reliability.md#leases--fencing).
5. **Per-extractor bearer tokens**, admin-minted (shown once, like personal access tokens), bound to the extractor id at mint time. A token can claim and write only its own kind — a compromised extractor cannot impersonate another's provenance. Tokens are delivery configuration (env), never manifest content.
6. **Registration rules.** Registration is an idempotent manifest upsert; a `version`/`model.version` change is recorded and is what makes files eligible for reprocessing ([F-009/FR-2](../features/F-009-reprocessing.md)). **Rendition kinds, derived-asset kinds, and embedding spaces are single-provider:** a manifest claiming a kind already claimed by a *different* extractor id is refused with a conflict naming the current claimant — "which `searchable-pdf` wins" can never arise. (Segments, metadata, and tags are deliberately multi-producer; every row is run-stamped.)
7. **Chaining** is declared, not coded: `accepts.derived_kinds` consumes another extractor's outputs (keyframes → image analysis), and an optional **`accepts.when` predicate on well-known metadata keys** gates routing on prior results — `tesseract-ocr` accepts `application/pdf` *when* `needs_ocr = true`, with `ocr_pages` passed as params. The same mechanism later carries the [F-018](../features/F-018-people.md) enablement gate.
8. **Result reuse:** at routing, an identical content hash already processed by the same extractor + version + model short-circuits to copying the completed run's outputs as this file's own rows, sourced from the cached run ([04 § identification](../specs/04-ingestion-pipeline.md#2-identification), [F-009/FR-8](../features/F-009-reprocessing.md)).

## Consequences

- The sandbox gets simpler and stronger ([ADR-0021](ADR-0021-extractor-sandbox-enforcement.md)): an extractor initiates every connection and listens on nothing, so it can live on a network with no inbound route and no egress.
- Extractor downtime degrades to jobs waiting in the queue — "facet pending", never "ingestion failed" — with no delivery state to reconcile.
- A remote extractor later is the same protocol over TLS with a different base URL; nothing about dispatch changes.
- The core serves extractor traffic (claims are cheap long-polls; asset uploads stream to staging) — acceptable at this deployment's scale, and the heavy CPU stays in the extractor containers by construction.
- Official extractors share an in-repo SDK for the claim/heartbeat/submit loop; third-party images need only the wire contract. The conformance kit exercises the protocol — including fencing rejection and duplicate-result convergence — against any image.
- Q30's phase-2 revisit closes with "no change": the lease/heartbeat defaults hold; the contract adds the cadence rule (heartbeat ≤ half lease) instead of new tuning.
