# ADR-0006 — Hierarchical tag vocabulary (DAG) with query-time expansion

**Status:** Accepted
**Date:** 2026-07-30

## Context

Tags are assigned manually and by auto-taggers. We want broad searches to find specifically-tagged files (`nature` should find files tagged `tree`). A tag can have **multiple parents** (`tree` under `nature → plant` *and* `garden → landscaping`), the taxonomy will be restructured for years, and auto-taggers emit open-vocabulary labels that drift across model versions. Governance: one global taxonomy, admin-managed.

Materializing ancestor tags onto files fails the multi-parent test: tagging `tree` would stamp on every ancestor path (`plant`, `nature`, `landscaping`, `garden`) — wrong tags on files (a forest photo gets `garden`), mass file updates on every restructure, and incoherent removal semantics (removing `nature` from a file still tagged `tree` contradicts the taxonomy). Hydrus's tag-parents/siblings model demonstrates the alternative.

## Decision

- The tag vocabulary is one **global, admin-governed DAG**: multi-parent allowed, cycles rejected on edit; **aliases** map synonyms and model labels to canonical tags.
- **Files carry a flat list of the most specific tags.** Ancestors are never materialized onto files.
- **Expansion happens at query time, downward**: `tag:nature` matches files tagged with any descendant, via a precomputed transitive-closure table (one indexed lookup); `exact: true` opts out. UI breadcrumbs (`nature › plant › tree`) are derived, never stored per file.
- Tags have a **status lifecycle**: `active` (approved vocabulary) · `suggested` (auto-tagger-created when no existing tag fits; **quarantined** — shown on the file detail clearly marked as a suggestion, excluded from search, facets, and autocomplete until an admin approves) · `rejected` (**soft-removed**: kept as a suppression record so later extraction runs cannot re-create the same suggestion; hard delete reserved for typo-grade mistakes with no history).
- The auto-tagger must **map into existing `active` tags first** (aliases + embedding similarity against tag names) and create a `suggested` tag only when nothing fits.

## Consequences

- Taxonomy restructuring is instant and touches zero file rows; multi-parent is safe by construction (expansion = set of descendants).
- Closure table must be maintained on taxonomy edits (cheap at realistic taxonomy sizes).
- Admins get a suggestion review queue; the search vocabulary stays strictly curated (suggested tags never leak into search).
- Model label drift is absorbed by the alias table instead of polluting the taxonomy.
