# F-002 — Hybrid Search (Exact + Semantic, Positional)

**Status:** Draft
**Priority:** P0
**Clients:** all
**Depends on:** F-003, F-004, F-005, F-006 (each adds searchable facets)
**Related specs:** [06-search](../specs/06-search.md), [02-domain-model](../specs/02-domain-model.md)

## Summary

One search API over everything the system knows: exact text/phrases, file names, typed metadata, tags — and semantic similarity via embeddings (text space + shared text–image space). Results point to positions (pages, timestamps) with snippets, are permission-filtered by construction, and explain why they matched.

## User stories

- As a user who knows a phrase from a document, I want to search it and be told *which file and which pages* (e.g. pages 1, 3, 7) so that I land directly on the right spot.
- As a user who remembers only the theme ("photo of my dog at the beach"), I want semantic search to find matching images even when no stored word matches my query.
- As a user, I want to combine modes and filters (tag `invoice`, year 2024, type PDF, plus a phrase) so that I can narrow reliably.
- As a user searching a video's content, I want a hit at *04:12* with the transcript snippet and a keyframe so that I can jump straight there.

## Functional requirements

- **FR-1** Exact mode: exact phrase over extracted text, exact/substring file-name search, exact metadata key/value, tag filters (hierarchy-expanding by default with `exact` opt-out — [ADR-0006](../decisions/ADR-0006-hierarchical-tags-dag.md)). Deterministic.
- **FR-2** Typed metadata filters support equality and ranges: datetime, number, string. Geo predicates: FR-17.
- **FR-3** Semantic mode: query is embedded per targeted space (`text-v1`, `clip-v1`) and matched via ANN; spaces are never cross-compared.
- **FR-4** Hybrid mode (default): keyword + semantic branches fused (RRF); exact-phrase hits rank above purely-semantic hits.
- **FR-5** Results are grouped per file with per-segment matches carrying anchors (page / timestamp / line / region) and highlighted snippets.
- **FR-6** Every result lists *why* it matched (signals incl. tag provenance and confidence).
- **FR-7** Permission filtering inside the query: no file the caller cannot read ever appears — not as snippet, facet count, or total.
- **FR-8** Version scope defaults to latest; `all versions` / time-scoped opt-in returns labeled old-version hits ([F-007](F-007-versioning.md)).
- **FR-9** Facets (type, tags, workspace, date buckets) returned with results; response includes honest `pending` counts while extraction backlog exists.
- **FR-10** p95 interactive query latency < 500 ms at target scale on CPU-only hardware; query-side embedding models stay memory-resident.
- **FR-11** Pagination is cursor-based and stable.
- **FR-12** Tags with status `suggested` are excluded from search matching, facets, and autocomplete until approved.
- **FR-13** Lifecycle state scope defaults to `live`: trashed items never appear in results, facets, counts, or autocomplete — enforced inside every query branch, including ANN ([F-014/FR-12](F-014-deletion-and-trash.md), [06](../specs/06-search.md#lifecycle-state-scope)). Opt-in `state: trashed | all` returns labeled hits, permission-checked like the trash listing.
- **FR-14** Folders are returned as folder-typed results, matched by name, tags, and metadata ([F-015/FR-10](F-015-folders.md)).
- **FR-15** Every file version carries exactly one core-assigned media `class` ∈ `image | video | audio | document | archive | other` ([04](../specs/04-ingestion-pipeline.md#2-identification)), filterable and returned as a facet — available the moment the file is listed, before any extraction ([F-001/FR-8](F-001-upload-and-import.md)).
- **FR-16** **Listing mode:** a request with no query text (filters only) is valid; results are ordered by an explicit `sort` — `name`, `size`, `mtime`, `ingested` (version registration time), or a range-typed well-known metadata key (`taken_at`, `duration`) — ascending or descending, entries missing the key ordered last, ties broken by file id; cursor pagination (FR-11) is stable under every sort. Folder-typed results join a listing only when the request filters on folder-matchable fields (name, tag, metadata) — a bare listing returns files. All permission, version, and lifecycle rules apply unchanged.
- **FR-17** Geo filters: bounding-box and radius predicates over geo-typed metadata (well-known key `gps` — [02](../specs/02-domain-model.md#metadataentry)) combine with every mode and filter, enforced inside each query branch like FR-7 and FR-13.
- **FR-18** **Geo grid aggregation:** a request may ask for aggregation over a bounding box at a zoom level; the response buckets matching files into grid cells, each with a count and one representative file id, returning individual results instead for cells at or below an item threshold (default 25, request-overridable up to a documented cap). Cell counts obey FR-7 and FR-13 exactly like facet counts: an unreadable or trashed file is absent from every cell and every count. This is what the [F-017](F-017-views.md) map layout executes.
- **FR-19** **Date-histogram aggregation:** a request may ask for a histogram over a date-typed sort key (`taken_at`, `mtime`, `ingested`) at `month` or `day` granularity; the response contains ordered buckets `{period, count}` covering the **entire** matched set (not the current page), plus one trailing bucket for items missing the key (mirroring FR-16's missing-last ordering). Bucket counts obey FR-7 and FR-13 exactly like facet and cell counts. This is what the [F-017](F-017-views.md) timeline layout executes — the scroll geometry and scrubber of timeline clients ([F-020/FR-1](F-020-mobile-library.md)) derive from it.
- **FR-20** **Compact listing projection:** a listing request (FR-16) may opt into a documented reduced per-item shape — file id, current version id, media class, intrinsic dimensions, the active sort-key value, duration (where applicable), and placeholder hash ([09](../specs/09-previews.md#thumbnails)) — returning exactly the same items in exactly the same order under exactly the same permission, version, and lifecycle rules as the full shape. Grid clients render whole months from it without fetching per-file detail; the version id is what lets them construct version-pinned, immutably-cacheable thumbnail URLs ([F-026/FR-26](F-026-offline-cache-and-prefetch.md), [09 § thumbnails](../specs/09-previews.md#thumbnails)).

## API surface

`POST /search` (query, mode, filters, sort, version scope, state scope, geo aggregation, date histogram, projection, cursor) — see result shape sketch in [06-search](../specs/06-search.md#result-shape-api-sketch). Views ([F-017](F-017-views.md)) store these requests verbatim and re-execute them through this same endpoint.

## Out of scope

Query-builder UI (a client concern) — saved searches themselves are specified in [F-017](F-017-views.md). Cross-instance search. Relevance-feedback learning.

## Open questions

[Q8 (result grouping/ranking tuning process)](../OPEN-QUESTIONS.md), [Q9 (embedding model selection)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- A phrase occurring on pages 1, 3, 7 of a PDF returns that file with exactly those page anchors and snippets.
- A spoken sentence in a video returns the video with a timestamp anchor within ±5 s of the utterance.
- "dog at the beach"-style query returns a beach-dog photo whose only textual traces are auto tags/labels — with zero query-word overlap.
- User B searching a phrase that only exists in files B cannot read gets zero results, zero facet counts, zero leaks (tested, not assumed).
- Exact filename search finds a file among millions in interactive time.
- Searching `tag:nature` finds a file tagged only `tree` (a descendant in the taxonomy); the same query with `exact: true` does not.
- A trashed file matching the query appears only with `state: trashed`/`all` (labeled), never by default — verified across results, facets, and counts like a permission leak test.
- `class: video` with no query text and `sort: mtime desc` lists every readable video newest-first in stable cursor order — including videos whose extraction is still pending.
- A radius filter (`gps` within 5 km of a point) combined with `class: image` and a tag filter returns only files satisfying all three.
- A bounding-box aggregation over a city returns grid cells whose counts cover only readable, live files; for a caller without access to any of them the same request returns no cells (leak test).
- A month-granularity histogram over `class: image` sorted by `taken_at` sums to the listing's total, buckets unreadable/trashed files in no period, and reports EXIF-less images in the trailing missing-key bucket (leak test like facets).
- A compact-projection listing of one month returns the same ids in the same order as the full listing, each with version id, class, dimensions, `taken_at`, and placeholder hash — and nothing else.
