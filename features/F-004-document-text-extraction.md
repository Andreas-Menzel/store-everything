# F-004 — Document Text Extraction & OCR

**Status:** Draft
**Priority:** P0
**Clients:** all
**Depends on:** F-001
**Related specs:** [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md), [05-extractor-contract](../specs/05-extractor-contract.md)

## Summary

Make document content searchable with positions. PDF is a decision tree, not one tool: born-digital PDFs yield their text layer directly (fast, accurate); scanned/photographed pages go through OCR (Tesseract baseline). Plain text, markdown, office documents, and code are extracted with line/section anchors. Output is always segments with anchors — originals are never modified (OCR text is never written back into the PDF).

## User stories

- As a user, I want the text of any PDF — scanned or digital — to be searchable so that "I know a phrase" always works.
- As a user, I want hits to tell me the page so that I don't scroll through 300 pages.

## Functional requirements

- **FR-1** Born-digital PDFs: text layer extracted per page → segments with page anchors.
- **FR-2** Scanned/low-text pages detected (per document or per page) and routed to OCR; OCR output segments carry page anchors and an OCR-confidence metadata flag.
- **FR-3** Office/text/markdown/code files extracted with line-range (or section) anchors.
- **FR-4** Detected document language stored as metadata (drives language-aware FTS).
- **FR-5** Extracted text feeds `text-v1` embeddings via chaining (`text-embed` extractor).
- **FR-6** Extraction failure (corrupt file, unsupported encoding) is a per-extractor status on the file, visible via API; the file remains stored and findable by name/metadata.
- **FR-7** Originals are byte-identical after extraction — verified by content hash.
- **FR-8** For scanned documents, a `searchable-pdf` **rendition** (original pages + embedded OCR text layer) is available via the renditions API ([ADR-0008](../decisions/ADR-0008-renditions.md)); the original remains untouched.

## API surface

Results via `GET /files/{id}/segments`; extraction status via `GET /files/{id}`; search via [F-002](F-002-hybrid-search.md); enriched copy via `GET /files/{id}/renditions/searchable-pdf`.

## Out of scope

Layout/table structure understanding, handwriting recognition, form-field extraction — candidates for future extractor plugins (the contract already supports them).

## Open questions

Per-page vs. per-document OCR routing threshold — implementation detail, decide with data. [Q9](../OPEN-QUESTIONS.md) covers model choices.

## Acceptance criteria

- A born-digital PDF's exact phrase is findable with correct page anchors; extraction is measurably faster than OCR on the same document.
- A scanned PDF (no text layer) becomes phrase-searchable via OCR with page anchors.
- A PDF's bytes on disk are identical before/after ingestion.
