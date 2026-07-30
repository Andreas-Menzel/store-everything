# Feature Index

One file per user-facing feature, `F-NNN-slug.md`, using [TEMPLATE.md](TEMPLATE.md). Requirements are numbered per feature (`FR-n`) so they can be referenced precisely (e.g. `F-002/FR-4`) from other docs, issues, and tests.

A feature is **specified here before it is implemented** — no feature file, no code. When a feature changes, its file is updated in the same change; FR ids are the traceability link into the test suite ([specs/11](../specs/11-engineering-standards.md)).

**Statuses:** `Draft` → `Review` → `Approved` → `Implemented` · plus `Deferred` (wanted later, API must not preclude)

| ID | Feature | Status | Priority |
|---|---|---|---|
| [F-001](F-001-upload-and-import.md) | File upload & workspace import | Draft | P0 |
| [F-002](F-002-hybrid-search.md) | Hybrid search (exact + semantic, positional) | Draft | P0 |
| [F-003](F-003-tagging.md) | Tagging (manual + auto, provenance) | Draft | P0 |
| [F-004](F-004-document-text-extraction.md) | Document text extraction & OCR | Draft | P0 |
| [F-005](F-005-image-analysis.md) | Image analysis (objects, scene, OCR, embeddings) | Draft | P0 |
| [F-006](F-006-av-transcription-and-keyframes.md) | Audio/video transcription & keyframes | Draft | P1 |
| [F-007](F-007-versioning.md) | File versioning & version search | Draft | P1 |
| [F-008](F-008-sharing-and-public-links.md) | Permissions, sharing & public links | Draft | P1 |
| [F-009](F-009-reprocessing.md) | Reprocessing with generations | Draft | P1 |
| [F-010](F-010-auto-sort-inbox.md) | Auto-sort inbox workspace | Deferred | P2 |
| [F-011](F-011-audit-trail.md) | Full audit trail | Draft | P1 |
| [F-012](F-012-live-updates.md) | Live updates (WebSocket) | Draft | P1 |
| [F-013](F-013-duplicate-detection.md) | Duplicate detection (exact) | Draft | P1 |

Priorities: **P0** — the product isn't this product without it · **P1** — needed for the v1 vision · **P2** — wanted, explicitly later.
