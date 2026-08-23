# F-028 — Thumbnails & Previews

**Status:** Approved
**Priority:** P0
**Clients:** all
**Depends on:** F-001, F-015
**Related specs:** [09-previews](../specs/09-previews.md), [02-domain-model](../specs/02-domain-model.md#derivedasset), [ADR-0008](../decisions/ADR-0008-renditions.md)

## Summary

Every file gets a uniform visual surface: one thumbnail endpoint with a fixed WebP size set (immutable, cacheable URLs), a compact placeholder hash so grids render instantly before any thumbnail arrives, a **preview descriptor** that tells clients what richer representations exist instead of making them guess by MIME type, and a renditions surface listing downloadable alternative forms of the whole file ([ADR-0008](../decisions/ADR-0008-renditions.md)). The tiers, formats, sources per type, and generation policy are fixed in [09-previews](../specs/09-previews.md); this feature is the API-and-client contract over them.

## User stories

- As a user, I want folder and library grids to show images of my files immediately so that browsing feels like a gallery, not a file manager.
- As a user, I want a PDF's pages viewable in the app — including jumping straight to one page — so that I don't download 300 pages to look at one.
- As a user, I want to download the enriched form of a file (a searchable PDF) while the original stays untouched so that I never trade fidelity for convenience.

## Functional requirements

- **FR-1** `GET /files/{id}/thumbnail?size=N` returns a WebP image whose longest edge is a member of the fixed set **{256, 512, 1024}**; `N` snaps **up** to the nearest set member (`N` above the largest returns the largest). No free-form resizing.
- **FR-2** For files of a thumbnail-source type (v1: images, PDFs, audio with embedded cover art — [09 § thumbnails](../specs/09-previews.md#thumbnails)), thumbnails at every set size exist after ingest processing completes, without any client having requested them.
- **FR-3** For a file without a thumbnail source, the endpoint returns a typed "no thumbnail" response — a problem-details `404` with a dedicated type, never an error placeholder image — and clients render a type icon.
- **FR-4** `?v={version_id}` pins the response to that version's thumbnail and carries `Cache-Control: private, max-age=31536000, immutable`; omitting `v` serves the current version **without** the `immutable` directive.
- **FR-5** Alongside each thumbnail, a compact placeholder (thumbhash-class, ≤ 64 bytes encoded) is stored as the well-known metadata key `placeholder_hash`; listing rows (folder children; later search results) return it inline together with the file's current version id, so clients construct pinned URLs and render aspect-correct blurred cells with zero extra requests.
- **FR-6** `GET /files/{id}/preview` returns a **descriptor** — JSON naming every preview asset that exists or is producible for this file (kind, format, dimensions/page count, URL) — and clients render from the descriptor, never from MIME-type guessing. A preview kind added by a new extractor appears in descriptors without a core change.
- **FR-7** Every page of a PDF is retrievable as a page image via the URL pattern the descriptor names; page 1 exists eagerly (it is the thumbnail source), later pages are rendered on first request and stored, so a repeat request serves the stored asset instead of re-rendering.
- **FR-8** `GET /files/{id}/renditions` lists the rendition kinds available or producible for the file; `GET /files/{id}/renditions/{kind}` downloads one.
- **FR-9** A file's original bytes are byte-identical (content hash) after all thumbnail, preview, and rendition generation, and `GET /files/{id}/content` always serves the original — never a rendition.
- **FR-10** Thumbnails, placeholders, previews, and renditions are served only to callers with read permission on the file; to any other caller the response is indistinguishable from a nonexistent file id.

## API surface

`GET /files/{id}/thumbnail?size=N&v=…` · `GET /files/{id}/preview` (descriptor; page-image URLs per FR-7) · `GET /files/{id}/renditions` · `GET /files/{id}/renditions/{kind}` · listing rows ([F-015](F-015-folders.md) children; [F-002](F-002-hybrid-search.md) results later) carry `current_version_id`, `placeholder_hash`, and thumbnail availability.

## Out of scope

Video thumbnails, scrub sheets, video preview transcodes, and audio waveforms — their source assets arrive with keyframes and A/V extraction ([F-006](F-006-av-transcription-and-keyframes.md), phase 3); the descriptor mechanism here already carries them. On-demand "Generate" UX for heavy rendition kinds (no heavy kind exists before phase 3). Attention/face-aware smart cropping ([F-018](F-018-people.md), later). Per-layout aspect variants — cropping is a client concern ([09](../specs/09-previews.md#thumbnails)).

## Open questions

None — the 512 px tier question (Q42) was resolved at phase-2 entry: the set is 256/512/1024.

## Acceptance criteria

- **AC-1** (FR-1, FR-2) After ingesting a corpus image, `?size=300` returns a WebP with longest edge 512; sizes 256, 512, and 1024 all exist in the derived store without any prior thumbnail request.
- **AC-2** (FR-3) A `.bin` fixture with no thumbnail source returns the typed "no thumbnail" problem; the web grid shows a type icon for it, not a broken image.
- **AC-3** (FR-4) A pinned request (`?v=`) responds with the `immutable` cache directive; the same request without `v` does not.
- **AC-4** (FR-5) A folder listing containing the corpus image returns `placeholder_hash` (≤ 64 bytes) and `current_version_id` inline in the child row.
- **AC-5** (FR-6, FR-7) For a 3-page corpus PDF, the descriptor names page images; requesting page 3 renders and stores it — a second request serves the stored bytes (asset present in the derived store after the first).
- **AC-6** (FR-8, FR-9) A scanned corpus PDF lists a `searchable-pdf` rendition ([F-004/FR-8](F-004-document-text-extraction.md)); downloading it yields embedded text, while `/content` still serves bytes matching the original's recorded hash.
- **AC-7** (FR-10) Bob requests the thumbnail, preview, and rendition of Alice's file by id: every response equals the nonexistent-id response.
