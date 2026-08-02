# 00 — Vision and Goals

**Status:** Draft

## Vision

A self-hosted personal cloud for 1–30 people that stores all their data and makes it *findable*. The user experience we optimize for:

- *"I know a phrase / name / date that is in the file"* → exact search returns the file **and the position**: document pages 1, 3 and 7; video at 04:12.
- *"I only know roughly what it is"* → semantic search: "photo of my dog at the beach" finds an image whose detected content is *dog, sand, ocean* without sharing a word with the query.

Both must work on ordinary self-hosted hardware, with all analysis running locally by default.

## Goals

| # | Goal |
|---|---|
| G1 | Store and organize files of **all types** in user-owned hierarchical folder structures (workspaces) |
| G2 | **Exact search**: text content, file names, metadata keys/values, dates, tags |
| G3 | **Semantic search**: embedding-based, across text and images (and video via keyframes/transcripts) |
| G4 | **Positional results**: page numbers, timestamps, highlighted snippets — to whatever degree is reasonable per file type |
| G5 | Automatic content analysis via **pluggable extractor containers**: OCR, text extraction, transcription, object/scene detection, preview generation |
| G6 | **Tags** with provenance (manual / auto / confirmed / rejected) and confidence scores |
| G7 | **Reprocessing**: when better models arrive, re-run extraction over all files; auto-derived data is replaced, manual data survives |
| G8 | **Multi-user**: accounts, workspaces, permissions, sharing (incl. public download links) |
| G9 | **File versioning**: search defaults to latest versions; older versions searchable on request |
| G10 | **Portability**: import an existing folder structure; the app can be removed at any time without data loss |
| G11 | **API-first**: every capability available via HTTP API before any UI exists |

## Non-goals (for now)

- Real-time collaborative editing of documents (this is not Google Docs).
- Cross-user storage deduplication semantics — if Alice and Bob upload the same image to their own workspaces, it exists twice logically (and physically on disk). Internal reuse of *extraction results* for identical content is an optimization, never a visible feature.
- Federation between instances.
- Being an email/photo/music *app* — we store, index, and search; specialized viewers can come later or be external API consumers.

## Deferred (explicitly wanted later, API must not preclude)

- Android/iOS apps with automatic photo/file sync — **now specified** ([F-019 – F-025](../features/README.md), [13-mobile-clients](13-mobile-clients.md)); no longer deferred.
- Desktop sync clients / WebDAV / S3-compatible access.
- External-source workspaces (GDrive, … — read-only, fully mirrored onto the server; Q16).
- A locally running AI agent as an API consumer.
- AI-driven auto-sorting of an "inbox" workspace beyond simple year/month rules.
- Remote (cloud) AI model backends as explicit opt-in.

## Scale and performance targets

| Dimension | Target |
|---|---|
| Accounts | 10–30, must be "easily done" |
| Total data | ~10 TB, incl. large video files |
| Hardware baseline | CPU-only server (no GPU assumed); optional GPU must be usable when present |
| Ingestion/analysis latency | May take time (minutes–hours per file is acceptable); background queue |
| Search latency | Fast and smooth once indexed — interactive (target: p95 < 500 ms for typical queries) |
| Deployment | Docker Compose on a single server (home machine or public server); extractors on a shared Docker network |

## Security posture

- Default deployment makes **zero external network calls** for analysis or search.
- Remote AI backends are per-extractor, explicit, visible configuration.
- Deployable on a public server → real authentication, per-file permissions, and permission-aware search (a search result snippet is a data leak if the user cannot open the file).
