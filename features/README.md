# Feature Index

One file per user-facing feature, `F-NNN-slug.md`, using [TEMPLATE.md](TEMPLATE.md). Requirements are numbered per feature (`FR-n`) so they can be referenced precisely (e.g. `F-002/FR-4`) from other docs, issues, and tests.

A feature is **specified here before it is implemented** — no feature file, no code. When a feature changes, its file is updated in the same change; FR ids are the traceability link into the test suite, enforced by the [traceability matrix](../specs/11-engineering-standards.md#requirement-traceability-the-matrix).

**Statuses:** `Draft` → `Review` → `Approved` → `Implemented` · plus `Deferred` (wanted later, API must not preclude). `Implemented` is **computed**, not claimed: a feature may carry it only while the traceability matrix shows every FR verified by its declared method.

| ID | Feature | Clients | Status | Priority |
|---|---|---|---|---|
| [F-001](F-001-upload-and-import.md) | File upload & workspace import | all | Draft | P0 |
| [F-002](F-002-hybrid-search.md) | Hybrid search (exact + semantic, positional) | all | Draft | P0 |
| [F-003](F-003-tagging.md) | Tagging (manual + auto, provenance) | all | Draft | P0 |
| [F-004](F-004-document-text-extraction.md) | Document text extraction & OCR | all | Draft | P0 |
| [F-005](F-005-image-analysis.md) | Image analysis (objects, scene, OCR, embeddings) | all | Draft | P0 |
| [F-006](F-006-av-transcription-and-keyframes.md) | Audio/video transcription & keyframes | all | Draft | P1 |
| [F-007](F-007-versioning.md) | File versioning & version search | all | Draft | P1 |
| [F-008](F-008-sharing-and-public-links.md) | Permissions, sharing & public links | all | Draft | P1 |
| [F-009](F-009-reprocessing.md) | Reprocessing with generations | all | Draft | P1 |
| [F-010](F-010-auto-sort-inbox.md) | Auto-sort inbox workspace | all | Deferred | P2 |
| [F-011](F-011-audit-trail.md) | Full audit trail | all | Draft | P1 |
| [F-012](F-012-live-updates.md) | Live updates (WebSocket) | all | Draft | P1 |
| [F-013](F-013-duplicate-detection.md) | Duplicate detection (exact) | all | Draft | P1 |
| [F-014](F-014-deletion-and-trash.md) | Deletion & trash | all | Draft | P1 |
| [F-015](F-015-folders.md) | Folders (identity, permissions, aggregates) | all | Draft | P0 |
| [F-016](F-016-archive-download.md) | Archive download (folders & selections) | all | Draft | P1 |
| [F-017](F-017-views.md) | Views (saved searches & library pages) | all | Draft | P1 |
| [F-018](F-018-people.md) | People (faces, persons, naming & account links) | all | Deferred | P2 |
| [F-019](F-019-mobile-connection.md) | Mobile: connection & device sessions | Android, iOS | Draft | P1 |
| [F-020](F-020-mobile-library.md) | Mobile: library, timeline & viewer | Android, iOS | Draft | P1 |
| [F-021](F-021-mobile-auto-upload.md) | Mobile: auto-upload (one-way device backup) | Android, iOS | Draft | P1 |
| [F-022](F-022-device-storage-reclaim.md) | Mobile: device storage reclaim | Android, iOS | Draft | P1 |
| [F-023](F-023-os-file-manager-integration.md) | Mobile: OS file-manager integration | Android, iOS | Draft | P2 |
| [F-024](F-024-offline-files-and-downloads.md) | Mobile: offline files & downloads | Android, iOS | Draft | P1 |
| [F-025](F-025-client-parity.md) | Mobile: native app parity (full web feature set) | Android, iOS | Draft | P1 |
| [F-026](F-026-offline-cache-and-prefetch.md) | Offline cache, instant navigation & prefetch | all | Draft | P1 |

Priorities: **P0** — the product isn't this product without it · **P1** — needed for the v1 vision · **P2** — wanted, explicitly later.

Priorities say what matters; *when* lives in [ROADMAP.md](../ROADMAP.md) — every feature is assigned to a phase (or the post-v1 pool) there, in the same change that creates or re-scopes it.

Clients: **all** = web + Android + iOS must surface it · anything narrower names the clients — semantics and obligations in [Writing FRs rule 9](#writing-frs).

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
9. **Client applicability is declared.** Every feature carries a `Clients:` header, mirrored in the index: `all` = web + Android + iOS must surface it. Anything narrower names the clients **and states its reason in one line** — a platform capability (MediaStore/PhotoKit access, OS trash dialogs, file-provider extension points, background workers) qualifies; convenience does not. FRs that differ per platform within one feature carry inline tags (`*(Android)*`, `*(iOS)*`). The tag is a **UI-surface obligation, never an API restriction** — endpoints stay client-agnostic ([08 § API-first](../specs/08-api-principles.md#api-first-concretely)); the parity bar for `all` features is [F-025/FR-1](F-025-client-parity.md).
