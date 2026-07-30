# ADR-0005 — v1 deployment: single server, shared Docker network

**Status:** Accepted
**Date:** 2026-07-29

## Context

Extractors are designed to "run anywhere" in principle, but distributed deployment (auth between hosts, file transfer, service discovery) is real complexity, and the v1 audience self-hosts on one machine. Source storage is a mounted folder (e.g. NAS share) on that machine.

## Decision

For v1, **all components — core API, orchestrator, PostgreSQL, and every extractor — run on a single server in one Docker Compose deployment, on a shared Docker network.** File references in the extractor contract resolve to a shared read-only volume mount and/or internal HTTP URLs on that network.

The extractor contract itself stays **location-agnostic** (pull-based references, async jobs — ADR-0002): moving extractors to another host later changes how references resolve (signed URLs over the network), not the API.

## Consequences

- Deployment is one `docker compose up`; no inter-host security model needed in v1.
- Extractor isolation is enforceable with Docker primitives (read-only mounts, `network: none`-style policies) — details in Q7.
- Total analysis throughput is bounded by one machine (acceptable: ingestion is allowed to be slow; GPU optional).
- We must resist contract shortcuts that only work co-located (e.g. absolute host paths as the *only* reference form), or the "runs anywhere" option quietly dies.
