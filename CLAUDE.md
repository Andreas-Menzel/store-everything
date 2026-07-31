# CLAUDE.md

## What this repository is

**Store Everything** — a self-hosted personal cloud for 1–30 users where search is the product: exact search returns positions (document pages 1, 3, 7; video at 04:12), semantic search finds "photo of my dog at the beach" via detected content. Every file is analyzed by pluggable extractor containers (OCR, transcription, object detection, …) running locally by default.

## Spec-driven development (mandatory workflow)

The documentation is the source of truth. Code must match the docs; divergence is never silent — if implementation must deviate, the same change updates the spec or records the point in `OPEN-QUESTIONS.md` / a new ADR.

For any feature or change, in this order — steps are not batched or skipped:

1. **Read the governing docs first**: the feature file (`features/F-NNN-*`), the affected `specs/`, related ADRs, and `OPEN-QUESTIONS.md`. Never implement against documents you haven't read.
2. **Analyze the related code** before planning.
3. **Plan the feature fully before implementing.** No feature file, no code — features are specified (summary, numbered FRs, acceptance criteria) before implementation. Planning may mean creating or updating spec docs.
4. **Propose in chat first, then get explicit approval.** Present the full design proposal in the conversation before changing any files. The user must approve new/updated spec docs before any code is written. Decisions deferred during discussion become rows in `OPEN-QUESTIONS.md`.
5. **Implement in reasonable phases.** Each phase is verified before the next begins: all requirements hold, all tests pass. Red stops the line.
6. **Tests bracket every change.** Before and after changing code, review the test suite: update tests where behavior changes, add tests where it makes sense.
7. **Update every affected document in the same change** — feature file, specs, OpenAPI schema, README, `OPEN-QUESTIONS.md`. Stale docs are worse than none.

The authoritative workflow, testing rules, Definition of Done, and CI gates live in [specs/11-engineering-standards.md](specs/11-engineering-standards.md).

## Document map and conventions

| Location | Content | Hard rules |
|---|---|---|
| `specs/00-…-11-….md` | Numbered system specs: vision, architecture, domain model, storage, ingestion, extractor contract, search, identity/permissions, API principles, previews, deployment, engineering standards | Cross-reference by relative link; update in the same change as any behavior change |
| `features/F-NNN-slug.md` | One file per user-facing feature, from `features/TEMPLATE.md`; FRs referenced as `F-002/FR-4` | FR authoring rules in [features/README.md](features/README.md#writing-frs); keep the index table in `features/README.md` in sync |
| `decisions/ADR-NNNN-slug.md` | Architecture decision records | **Immutable once accepted** — changing a decision means a new ADR that supersedes the old one; keep the index in `decisions/README.md` in sync |
| `OPEN-QUESTIONS.md` | Deferred decisions Q1, Q2, … | Numbers are stable, never reused or renumbered; resolved questions keep their row, marked ✅ with a link to the deciding ADR/spec |

Rules that bite when writing FRs (full list in [features/README.md](features/README.md#writing-frs)):

- **Atomic and falsifiable at a boundary** (API response, state on disk, event log) — name the test that would fail; internal design belongs in specs/ADRs.
- **Normative language**; vague words ("gracefully", "fast", "robust", "properly") are banned — use numbers or linked definitions.
- **Negative space is its own FR** — what must never happen (permission leaks, modified originals) gets its own requirement and negative test.
- **Ids are append-only** — removed FRs stay as tombstones; changing meaning = new FR + tombstone. Cross-feature references cite exact ids (`F-014/FR-12`).
- FRs not verifiable by a plain deterministic test declare a method inline: `*(verify: benchmark)*`, `*(verify: fault-injection)*`, `*(verify: drill)*`.
- Feature statuses: `Draft` → `Review` → `Approved` → `Implemented` (+ `Deferred`). `Implemented` is **computed** by the traceability matrix, never claimed.
