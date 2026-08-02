# F-009 — Reprocessing with Generations

**Status:** Draft
**Priority:** P1
**Clients:** all
**Depends on:** F-003, F-004, F-005, F-006
**Related specs:** [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md#reprocessing-generations), [ADR-0004](../decisions/ADR-0004-tag-provenance-and-reprocessing.md)

## Summary

When a better model or extractor version arrives, re-run extraction — over everything or selectively (one extractor, one file type, one workspace). New results replace the previous generation's `auto` outputs; manual and confirmed user input survives untouched; rejected tags stay rejected. The previous generation is kept until the new one proves itself (rollback).

## User stories

- As an admin, I want to upgrade the image model and re-analyze all images (not the other 9 TB) so that tags improve without redoing everything.
- As an admin, I want to roll back a reprocess whose new model turned out worse.
- As a user, I want my manual tags and corrections guaranteed to survive any reprocess.

## Functional requirements

- **FR-1** Trigger scopes: all files · by extractor · by MIME type/file class · by workspace · single file. Combinable.
- **FR-2** Eligibility is version-aware: "rerun where extractor/model version < X" — automatic candidate listing when an extractor registers a new version.
- **FR-3** Runs execute as a new generation through the normal queue at low priority (fresh ingestion outranks reprocessing).
- **FR-4** Per file, the generation swap of `auto` outputs (tags, metadata, segments, embeddings) is atomic: search never sees a half-swapped file.
- **FR-5** `manual`/`confirmed` untouched; `rejected` suppressions honored (ADR-0004).
- **FR-6** Previous generation retained until pruned; rollback per scope restores it.
- **FR-7** Progress, throughput, ETA, and failures queryable via API; reprocess is pausable/resumable/cancelable.
- **FR-8** Identical content hash + same extractor+model version ⇒ result reuse instead of recompute.

## API surface

`POST /reprocess` (scope, extractor, target-version) · `GET /reprocess/{id}` (progress) · `POST /reprocess/{id}/pause|resume|cancel|rollback` · `GET /extractors` (versions, eligible-file counts). Admin-only.

## Out of scope

Automatic quality evaluation of old vs. new model (confirm/reject data could enable this later). Scheduling policies beyond priority (nice-to-have).

## Open questions

Generation pruning policy defaults (keep 1 previous? age-based?) — decide with storage data.

## Acceptance criteria

- Upgrading `image-vision` and reprocessing `image/*` touches only image-derived data; documents' data is byte-identical.
- A `confirmed` tag and a `rejected` tag are in the same state before and after a full reprocess with a new model.
- Rollback after a completed reprocess restores the previous generation's tags/metadata exactly.
- Search during reprocessing returns each file's old *or* new generation, never a mixture for one file.
