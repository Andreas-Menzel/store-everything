# F-002 — Hybrid Search (Exact + Semantic, Positional)

**Status:** Draft
**Priority:** P0
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
- **FR-2** Typed metadata filters support equality and ranges: datetime, number, geo(later), string.
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

## API surface

`POST /search` (query, mode, filters, version scope, cursor) — see result shape sketch in [06-search](../specs/06-search.md#result-shape-api-sketch).

## Out of scope

Query-language UI niceties (saved searches, query builder) — later, as API consumers. Cross-instance search. Relevance-feedback learning.

## Open questions

[Q8 (result grouping/ranking tuning process)](../OPEN-QUESTIONS.md#q8), [Q9 (embedding model selection)](../OPEN-QUESTIONS.md#q9).

## Acceptance criteria

- A phrase occurring on pages 1, 3, 7 of a PDF returns that file with exactly those page anchors and snippets.
- A spoken sentence in a video returns the video with a timestamp anchor within ±5 s of the utterance.
- "dog at the beach"-style query returns a beach-dog photo whose only textual traces are auto tags/labels — with zero query-word overlap.
- User B searching a phrase that only exists in files B cannot read gets zero results, zero facet counts, zero leaks (tested, not assumed).
- Exact filename search finds a file among millions in interactive time.
- Searching `tag:nature` finds a file tagged only `tree` (a descendant in the taxonomy); the same query with `exact: true` does not.
