# Architecture Decision Records

Significant decisions, one file each, numbered and immutable once accepted. To change a decision, write a new ADR that supersedes the old one.

**Statuses:** `Proposed` (recommended, awaiting confirmation) · `Accepted` · `Superseded by ADR-XXXX`

| ADR | Title | Status |
|---|---|---|
| [ADR-0001](ADR-0001-postgresql-single-datastore.md) | PostgreSQL as the single datastore | Accepted |
| [ADR-0002](ADR-0002-extractor-containers-fixed-api.md) | Extractors as Docker containers behind a fixed, pull-based API | Accepted |
| [ADR-0003](ADR-0003-files-on-disk-source-of-truth.md) | Files on disk are the source of truth; all app data is a derived layer | Accepted |
| [ADR-0004](ADR-0004-tag-provenance-and-reprocessing.md) | Tag provenance model and reprocessing rules | Accepted |
| [ADR-0005](ADR-0005-single-server-docker-network.md) | v1 deployment: single server, shared Docker network | Accepted |
| [ADR-0006](ADR-0006-hierarchical-tags-dag.md) | Hierarchical tag vocabulary (DAG) with query-time expansion | Accepted |
| [ADR-0007](ADR-0007-unified-event-log.md) | Unified event log via transactional outbox (audit, feed, live updates) | Accepted |
| [ADR-0008](ADR-0008-renditions.md) | Renditions: enriched alternative file forms as derived assets | Accepted |
| [ADR-0009](ADR-0009-external-traefik-edge.md) | Edge via an existing external Traefik instance | Accepted |
| [ADR-0010](ADR-0010-crash-only-execution-model.md) | Crash-only execution: durable operations, leases + fencing, idempotent effects | Accepted |
| [ADR-0011](ADR-0011-person-recognition-architecture.md) | Person recognition: opt-in face extractor, core-owned per-owner identity resolution | Accepted |
| [ADR-0012](ADR-0012-python-fastapi-core-stack.md) | Core service stack: Python + FastAPI, PostgreSQL via SQLAlchemy Core | Accepted |
| [ADR-0013](ADR-0013-owned-operation-layer.md) | The operation layer is ours: no job-queue library | Accepted |
| [ADR-0014](ADR-0014-vue-frontend-stack.md) | Web UI stack: Vue 3 SPA with a generated API client | Accepted |
| [ADR-0015](ADR-0015-ground-truth-corpus-strategy.md) | Ground-truth corpus: synthetic-first, curated for realism, manifest-governed | Accepted |
| [ADR-0016](ADR-0016-license-and-third-party-compliance.md) | AGPL-3.0, and third-party license compliance as a CI gate | Accepted |
| [ADR-0017](ADR-0017-resumable-upload-protocol.md) | Uploads speak the IETF resumable-upload protocol, implemented in-app | Accepted |
| [ADR-0018](ADR-0018-workspace-layout-and-adoption.md) | Workspace layout: managed and adopted roots, one control directory | Accepted |
| [ADR-0019](ADR-0019-source-tree-semantics.md) | Source-tree semantics: names, symlinks, change detection, filesystem requirements | Accepted |

## Template

```markdown
# ADR-NNNN — Title

**Status:** Proposed | Accepted | Superseded by ADR-XXXX
**Date:** YYYY-MM-DD

## Context
What situation/forces make a decision necessary.

## Decision
The decision, stated actively ("We will …").

## Consequences
What becomes easier, what becomes harder, what we commit to.
```
