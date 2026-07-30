# ADR-0008 — Renditions: enriched alternative file forms as derived assets

**Status:** Accepted
**Date:** 2026-07-30

## Context

Users want both the untouched original **and** enriched forms: a searchable PDF with the OCR text layer embedded, a video with subtitles from the transcript. Originals are immutable — **no exceptions, PDF included**. The mechanism must not be coupled to the PDF format: any extractor or future plugin should be able to offer new enriched forms. Paperless-ngx's original-plus-archive-PDF/A validates the pattern; Plex-style on-demand generation shows how to bound storage cost.

## Decision

- A **rendition** is a derived asset class: a downloadable **alternative form of the whole file**, produced by an extractor. The original always remains byte-identical and is always what `/files/{id}/content` serves.
- Extractors declare `produces: renditions` and list kinds in their manifest: `{kind, format, label}` (e.g. `searchable-pdf`, `subtitles-srt`, `subtitled-video`). The *kind vocabulary is open* (plugins add new ones); the *mechanism is fixed*.
- API: `GET /files/{id}/renditions` lists available/producible forms; `GET /files/{id}/renditions/{kind}` downloads.
- Renditions are ordinary derived data: tied to file version + extractor + generation, regenerable, deletable, never confused with the source.
- Renditions are **independent alternatives** in v1 — no stacking/composition chains (OCR layer + watermark). Nothing in the contract precludes adding composition later.
- Generation policy: cheap kinds (`.srt`, searchable PDFs for documents) may be generated eagerly; **heavy kinds** (muxed subtitled copy of a 4 GB video) are generated **on demand** ("Generate" button → async job → cached, with cache eviction). Final policy: Q15.

## Consequences

- "Original and enriched download" works for every file type through one mechanism; PDF is not a special case anywhere in the core.
- Storage stays bounded: heavy renditions are a cache, not a second archive.
- Small, contained extension to the extractor contract and API surface.
- On-demand kinds introduce "wait for generation" UX (job + notification) — acceptable, matches the async-everything design.
