# F-006 — Audio/Video Transcription & Keyframes

**Status:** Draft
**Priority:** P1
**Depends on:** F-001, F-005
**Related specs:** [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md)

## Summary

All audio content — videos, voice messages, MP3s, recordings — is transcribed locally (Whisper-class model) into timestamped segments. Videos additionally yield keyframes, which flow through the full image pipeline (objects, scene, OCR) with timestamp anchors. Result: a spoken sentence, a visible object, or on-screen text in a video is findable *at its moment* ("video Y at 04:12").

## User stories

- As a user, I want to search words spoken in a video/voice message and jump to the exact timestamp.
- As a user, I want a visual moment ("whiteboard with architecture diagram") findable inside a long video.

## Functional requirements

- **FR-1** Audio in any common container/codec is transcribed locally into text segments with start/end timestamps.
- **FR-2** Spoken-language detection → metadata; multi-language content handled at least per-file.
- **FR-3** Video keyframes extracted (scene-change based + interval fallback) as derived assets with timestamps.
- **FR-4** Keyframes are chained into image analysis ([F-005](F-005-image-analysis.md)) and OCR; all resulting metadata/tags/segments carry the source timestamp.
- **FR-5** Transcripts retrievable in full via API (`/files/{id}/segments`) — they double as captions/subtitle source.
- **FR-6** Transcription is CPU-feasible (slow is acceptable) and GPU-accelerated when present; long jobs report progress.
- **FR-7** Media technical metadata (duration, codecs, resolution, bitrate) → typed metadata.
- **FR-8** Search hits on transcript or keyframe return the timestamp anchor and, for keyframe hits, the keyframe preview ([F-002/FR-5](F-002-hybrid-search.md)).
- **FR-9** The transcript is exportable as a subtitle **rendition** (`.srt`/`.vtt`); a muxed subtitled video copy is a heavy, on-demand rendition (generation policy Q15 — [ADR-0008](../decisions/ADR-0008-renditions.md)).

## API surface

No new endpoints; derived keyframes via `GET /files/{id}/preview`-family, transcripts via `GET /files/{id}/segments`.

## Out of scope

Speaker diarization ("who said it"), music/audio-event classification, subtitle-file (.srt) *import* — future extractor plugins.

## Open questions

Keyframe density defaults (storage vs. recall trade-off at 10 TB with lots of video); [Q9](../OPEN-QUESTIONS.md#q9) for model choice.

## Acceptance criteria

- A phrase spoken at ~04:12 in a video returns that video with an anchor within ±5 s.
- A voice message (e.g. OGG/Opus) is searchable by its spoken content.
- An object visible only mid-video is findable semantically and returns a timestamped keyframe.
- A 2 h video on CPU-only hardware completes transcription (eventually) with visible progress, without blocking any other ingestion.
