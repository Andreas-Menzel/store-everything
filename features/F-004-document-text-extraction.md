# F-004 — Document Text Extraction & OCR

**Status:** Approved
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

## Staging

**Phase 2 delivers FR-1, FR-2, FR-4, FR-6, FR-7 and FR-8** across three extractors: `pdf-text` (the
per-page decision tree and the `needs_ocr`/`ocr_pages` signal it writes), `text-plain` (text,
markdown, code and CSV), and `tesseract-ocr` (the OCR half of FR-2, and the `searchable-pdf`
rendition of FR-8). **FR-3** lands for text, markdown and code with line-range anchors.

Two parts wait for **phase 3**, both because a phase-3 component is what makes them possible:

- **Embeddings** (FR-5): segments feeding `text-v1` needs the `text-embed` extractor and the
  embedding infrastructure around it.
- **Office documents** (FR-3's office half) need an anchor that can honestly be used. Reading a `.docx`'s text is a wheel
  away; saying *where* in it a hit is, is not: the anchor vocabulary is fixed at `page`, `time`,
  `line`, `sheet`, `region`, `whole` ([02 § Segment](../specs/02-domain-model.md#segment)), and a
  word-processing document has none of those until something lays it out into pages. This FR's
  parenthetical "(or section)" invites a kind the domain model does not define — recorded as
  [Q64](../OPEN-QUESTIONS.md). [05](../specs/05-extractor-contract.md)'s answer is `office-convert`:
  LibreOffice renders one `pdf` rendition that chains into `pdf-text`, so a hit's page is the page
  the preview shows — one conversion serving the anchor *and* the preview. It travels with phase
  3's heavy toolchain, where LibreOffice's ~400 MB image belongs.

So FR-3 is verified for text and stays unverified for office formats rather than being claimed on
an anchor that would be a guess.

## Open questions

Routing is per page ([ADR-0020](../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md): `pdf-text` writes `needs_ocr`/`ocr_pages`, the OCR manifest predicate binds to them); the exact garbled-text thresholds inside `pdf-text`'s decision tree are implementation detail, tuned against corpus fixtures. [Q9](../OPEN-QUESTIONS.md)'s documents part is resolved ([05 § built-in extractors](../specs/05-extractor-contract.md#built-in-extractors-default-installation-all-local)); its embedding/vision/speech part remains for phase 3.

## Acceptance criteria

- A born-digital PDF's exact phrase is findable with correct page anchors; extraction is measurably faster than OCR on the same document.
- A scanned PDF (no text layer) becomes phrase-searchable via OCR with page anchors.
- A PDF's bytes on disk are identical before/after ingestion.
