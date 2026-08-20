# F-005 — Image Analysis (Objects, Scene, OCR, Embeddings)

**Status:** Draft
**Priority:** P0
**Clients:** all
**Depends on:** F-001, F-003
**Related specs:** [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md), [05-extractor-contract](../specs/05-extractor-contract.md)

## Summary

Every image — uploaded photos and video keyframes alike — is analyzed by locally running models: object detection (with bounding boxes and confidences), scene classification, OCR for visible text, and CLIP-space embeddings for semantic search. Detections are stored as structured metadata *and* surfaced as `auto` tags. All local by default; CPU-capable, GPU-accelerated when available.

## User stories

- As a user, I want photos to be findable by what's *in* them ("dog", "beach", "birthday cake") without tagging anything myself.
- As a user, I want text visible in photos (signs, receipts, screenshots) to be searchable.
- As a user, I want "photo of my dog at the beach" to work even though I never typed those words anywhere.

## Functional requirements

- **FR-1** Object detection on every image: labels + bounding boxes + confidences → metadata; labels above threshold → `auto` tags with confidence.
- **FR-2** Scene/context classification (indoor/outdoor, beach, office, …) → metadata + `auto` tags.
- **FR-3** OCR on every image ([F-004](F-004-document-text-extraction.md) extractor, chained) → text segments (image-region anchors where available).
- **FR-4** CLIP-space (`clip-v1`) embedding per image → semantic text-to-image search.
- **FR-5** EXIF and technical metadata (taken-at, GPS, camera, dimensions) → typed metadata for exact/range search.
- **FR-6** Identical pipeline applies to video keyframes via chaining ([F-006](F-006-av-transcription-and-keyframes.md)), with timestamp anchors.
- **FR-7** All models run locally; the extractor runs CPU-only and uses a GPU when present (same container).
- **FR-8** Tag-emission confidence threshold is instance-configurable; below-threshold detections stay in metadata (searchable) without becoming tags.

## API surface

Results via `GET /files/{id}` (metadata, tags), `GET /files/{id}/segments` (OCR), search via [F-002](F-002-hybrid-search.md).

## Out of scope

Face recognition / person identification — privacy-sensitive, therefore its own explicitly opt-in feature: [F-018](F-018-people.md) (ships disabled; instance + per-workspace enablement gates — [ADR-0011](../decisions/ADR-0011-person-recognition-architecture.md)). Duplicate-photo detection UX (hashes exist; the feature comes later).

## Open questions

[Q9 (which local models)](../OPEN-QUESTIONS.md), threshold defaults.

## Acceptance criteria

- A beach-dog photo, ingested with zero user input, is returned for the semantic query "dog playing at the sea" and carries `auto` tags like `dog`, `beach` with confidences.
- A screenshot containing an error message is findable by exact phrase from that message.
- Photos filterable by `taken-at` year range from EXIF alone.
