# 05 — Extractor Contract (Fixed Plugin API)

**Status:** Draft
**Related ADRs:** [ADR-0002](../decisions/ADR-0002-extractor-containers-fixed-api.md), [ADR-0005](../decisions/ADR-0005-single-server-docker-network.md)

## Principles

An **extractor** is a separate Docker container that adds one analysis capability to the system. Extractors and the core share **only one fixed API** — this is the plugin boundary that makes the system extensible without touching the core.

1. **Fixed, versioned contract.** The extractor API itself carries a version (`extractor-api/v1`); extractors declare which contract version they speak.
2. **Pull-based file access.** The core never streams file bytes *into* an extractor. It hands over a **read-only file reference**; the extractor pulls the bytes (or ranges) it needs. For v1 (same server, shared Docker network — ADR-0005) references resolve to a shared read-only volume mount and/or internal HTTP URL. The contract must stay location-agnostic so remote extractors later only change how references resolve, not the API.
3. **Async job lifecycle.** Jobs run seconds to hours. Accept → progress → result, with idempotency keys and cancellation.
4. **Declared capabilities.** Extractors are *registered*, not broadcast to. Routing is driven by their manifest.
5. **Isolated by default.** Read-only file access, no outbound network unless explicitly configured (a remote-AI extractor is exactly that: an explicitly network-enabled extractor). Enforcement mechanics: Q7.

## Manifest (registration)

Each extractor exposes/declares:

```yaml
id: image-vision
version: 1.4.0                # extractor implementation version
api_version: v1               # extractor contract version
model:
  name: yolo-something
  version: 8.2                # model version — part of every result's provenance
accepts:
  mime_types: ["image/*"]
  derived_kinds: ["keyframe"] # can also consume other extractors' outputs (chaining)
produces: [metadata, tags, embeddings]   # of: metadata | text_segments | tags | embeddings | derived_assets | renditions
renditions: []                # if producing renditions: [{kind, format, label}] (ADR-0008)
embedding_spaces: ["clip-v1"] # if producing embeddings
cost_class: heavy             # light | medium | heavy — scheduler hint
gpu: optional                 # none | optional | required
network: none                 # none | outbound (explicit opt-in, visible to admin)
```

Changing `version` or `model.version` is what makes files eligible for reprocessing by this extractor.

## Container requirements (hardening)

Extractor images — official and third-party — follow the same container baseline as the core:

- Run as a **non-root** user; base image **pinned by digest**; no secrets in image layers.
- File access is read-only (principle 2); **no outbound network** unless the manifest declares `network: outbound` — an explicit, admin-visible opt-in (enforcement mechanics: Q7).
- All configuration via environment variables. Credentials for network-enabled extractors (remote AI backends) are env-provided at deploy time — **never in the manifest, never logged** (Q19).
- The official extractor images are dependency- and image-scanned in CI ([11](11-engineering-standards.md#ci-pipeline-the-enforcement-list)).

## Job lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: orchestrator creates job
    queued --> running: extractor accepts
    running --> running: progress updates
    running --> succeeded: results submitted
    running --> failed: error (retryable → re-queued with backoff)
    failed --> queued: retry (bounded)
    failed --> dead_letter: retries exhausted (API-visible)
    running --> cancelled: reprocess superseded / user cancel
    succeeded --> [*]
```

A job request contains: job id, idempotency key, file reference(s), file version id + content hash, MIME type, generation, and extractor-specific params. Whether dispatch is push (core → extractor HTTP call) or poll (extractor pulls from queue) is Q5, together with chaining declaration details.

Restarts are routine, not failures: delivery is **at-least-once** — on orchestrator shutdown (SIGTERM: upgrade, restart) unfinished jobs are re-queued, and result submission is deduplicated via the job's idempotency key, so a job that completes twice persists once. Extractors receive cancellation for superseded jobs.

## Result envelope

One structured result per job; all content is optional, per the extractor's `produces` declaration:

| Output kind | Shape (essentials) |
|---|---|
| `metadata` | typed key–value entries: `{key, type, value, confidence?}` — EXIF, duration, page count, detected objects w/ bounding boxes, scene labels, language, … Types and **well-known keys** (`gps`, `taken_at`, …) are defined in [02-domain-model.md](02-domain-model.md#metadataentry), so core features (map, timeline) bind to keys, not to specific extractors |
| `text_segments` | `{text, anchor}` list — anchor is page number, time range, line range, or image region ([02-domain-model.md](02-domain-model.md#segment)). This is what powers positional search results. |
| `tags` | `{name, confidence}` list — stored as `auto` tags with full provenance |
| `embeddings` | `{space, segment_ref, vector}` list |
| `derived_assets` | previews/thumbnails, keyframes (with timestamps), transcript files, waveforms — uploaded to the derived store |
| `renditions` | downloadable **alternative forms of the whole file** — searchable PDF (embedded OCR layer), subtitled video, `.srt` ([ADR-0008](../decisions/ADR-0008-renditions.md)). Originals are never replaced. |

Every persisted row is stamped by the core with: extractor id, extractor version, model version, extraction run, generation.

## Built-in extractors (default installation, all local)

The app must be usable for **all common file types out of the box** — plugins extend, they are not required for baseline usefulness.

| Extractor | Accepts | Produces | Notes |
|---|---|---|---|
| `basic-metadata` | `*/*` | metadata | size, times, EXIF, media info (duration, codecs, dimensions) |
| `pdf-text` | PDF | text_segments (page anchors), metadata | born-digital text layer; flags scanned pages for OCR |
| `tesseract-ocr` | images, scanned PDFs, keyframes | text_segments, renditions | OCR on all images incl. video keyframes; emits `searchable-pdf` rendition for scanned documents |
| `text-plain` | text, markdown, office docs, code | text_segments | line/section anchors |
| `image-vision` | images, keyframes | metadata (objects+boxes, scene), tags, embeddings (`clip-v1`) | local model, CPU-capable, GPU-optional |
| `av-transcribe` | audio (voice, MP3, …), video | text_segments (timestamp anchors), metadata (language), renditions | Whisper-class, local; emits `.srt`/`.vtt` subtitle rendition |
| `video-keyframes` | video | derived_assets (keyframes w/ timestamps, scrub sheet) | feeds image-vision + OCR via chaining; scrub sheet per [09-previews.md](09-previews.md) |
| `text-embed` | text_segments (chained) | embeddings (`text-v1`) | semantic doc search |
| `preview-gen` | `*/*` | derived_assets (thumbnails, image previews) | thumbnails + image previews; type-specific preview assets live with their type's extractors ([09-previews.md](09-previews.md)) |

## Compatibility rules

- Core must tolerate unknown extra fields in results (forward compatibility).
- An extractor that speaks an older contract version keeps working within `v1.x`.
- Extractor failure or absence never corrupts state: worst case is missing facets, visible in per-file extraction status.
