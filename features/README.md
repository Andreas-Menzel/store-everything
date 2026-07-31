# Feature Index

One file per user-facing feature, `F-NNN-slug.md`, using [TEMPLATE.md](TEMPLATE.md). Requirements are numbered per feature (`FR-n`) so they can be referenced precisely (e.g. `F-002/FR-4`) from other docs, issues, and tests.

A feature is **specified here before it is implemented** — no feature file, no code. When a feature changes, its file is updated in the same change; FR ids are the traceability link into the test suite, enforced by the [traceability matrix](../specs/11-engineering-standards.md#requirement-traceability-the-matrix).

**Statuses:** `Draft` → `Review` → `Approved` → `Implemented` · plus `Deferred` (wanted later, API must not preclude). `Implemented` is **computed**, not claimed: a feature may carry it only while the traceability matrix shows every FR verified by its declared method.

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
| [F-014](F-014-deletion-and-trash.md) | Deletion & trash | Draft | P1 |
| [F-015](F-015-folders.md) | Folders (identity, permissions, aggregates) | Draft | P0 |
| [F-016](F-016-archive-download.md) | Archive download (folders & selections) | Draft | P1 |
| [F-017](F-017-views.md) | Views (saved searches & library pages) | Draft | P1 |

Priorities: **P0** — the product isn't this product without it · **P1** — needed for the v1 vision · **P2** — wanted, explicitly later.

## Writing FRs

The template gives requirements a place; these rules make them worth testing against ([specs/11 § Testing](../specs/11-engineering-standards.md#testing) holds the enforcement side):

1. **Atomic.** One requirement per FR — if it needs an "and also", split it. Each id must be independently testable and referenceable.
2. **Falsifiable, at a boundary.** An FR states observable behavior at a system boundary (API response, state on disk, event log) — never internal design ("uses a queue" belongs in specs/ADRs). Authoring check: name the test that would fail if the FR were violated. If you can't, it isn't an FR yet.
3. **Normative language.** MUST-strength by default; a skipped SHOULD needs a recorded reason. Words that can't fail a test — "gracefully", "properly", "appropriately", "fast", "reasonable", "robust", "user-friendly" — are banned; use numbers or linked definitions instead.
4. **Negative space is explicit.** What must *never* happen (permission leaks, modified originals, trashed items surfacing) is written as its own FR, because it needs its own negative test — [F-002/FR-7](F-002-hybrid-search.md) is the house style.
5. **Verification method declared.** An FR that a plain deterministic test cannot verify carries a marker after its id — `*(verify: benchmark)*`, `*(verify: fault-injection)*`, `*(verify: drill)*`; unmarked FRs default to `test` ([specs/11 § Verification methods](../specs/11-engineering-standards.md#verification-methods-per-fr)). Acceptance criteria for `benchmark` FRs state metric and threshold, not a single example.
6. **Ids are immutable.** FR numbers are append-only — never reused, never renumbered. A removed FR keeps its number as a tombstone: `**FR-3** *(removed — see ADR-00xx)*`. Changing an FR's *meaning* is a new FR plus a tombstone on the old one, so existing tests can't silently guard the wrong requirement; wording-only edits are fine.
7. **Cross-feature dependencies cite exact ids** — [F-014/FR-12](F-014-deletion-and-trash.md), not "see the deletion feature" — so the matrix and readers can follow them.
8. **Acceptance criteria are numbered** (`AC-n`), name the FR(s) they demonstrate, and use concrete inputs and outputs. ACs are worked examples — the script for the feature's integration/E2E tests; FRs are the rule.
