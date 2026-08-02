# F-003 — Tagging (Manual + Auto, Provenance)

**Status:** Draft
**Priority:** P0
**Clients:** all
**Depends on:** —
**Related specs:** [02-domain-model](../specs/02-domain-model.md#tag--filetag), [ADR-0004](../decisions/ADR-0004-tag-provenance-and-reprocessing.md)

## Summary

Every file can carry multiple tags. Users tag manually; extractors tag automatically with confidence scores. Provenance (`manual` / `auto` / `confirmed` / `rejected`) is always visible, user curation always wins over machines, and corrections stick across reprocessing. Tags belong to the file — everyone with read permission sees the same tags.

## User stories

- As a user, I want to add and remove my own tags on any file I can write to so that I can organize my way.
- As a user, I want to see which tags were machine-assigned (and how confident the model was) so that I know what to trust.
- As a user, I want to confirm a correct auto tag so that it becomes permanent, and reject a wrong one so that it never comes back.
- As a collaborator with write permission on Alice's file, I want my tag edits to be visible to Alice so that we share one truth.

## Functional requirements

- **FR-1** Files support multiple tags, always as a **flat list of the most specific tags** — ancestors are never materialized onto files. Tag names live in one global, admin-governed hierarchical vocabulary (DAG with aliases — [ADR-0006](../decisions/ADR-0006-hierarchical-tags-dag.md)); names are normalized (case/whitespace).
- **FR-2** Manual add/remove for users with `write` on the file; recorded with user id.
- **FR-3** Auto tags carry provenance `auto`, confidence (when provided), and full source stamp (extractor + version + model + generation) — exposed in every API response.
- **FR-4** Confirming an auto tag sets `confirmed`; it is thereafter treated as user truth (reprocessing-immune).
- **FR-5** Removing an auto tag records `rejected`; no future extraction generation may re-add that tag to that file.
- **FR-6** Reprocessing replaces only `auto` tags (generation swap, ADR-0004); `manual`/`confirmed` survive verbatim.
- **FR-7** Tags are searchable and facetable, filterable by provenance ([F-002/FR-1](F-002-hybrid-search.md)).
- **FR-8** Tag autocomplete supports prefix search with usage counts, plus optional embedding-similarity suggestions (typing `car` also surfaces `vehicle`) — computed over `active` tags only.
- **FR-9** Tags/metadata are per-file shared state: concurrent edits resolve last-write-wins with the full change recorded in the audit trail ([F-011](F-011-audit-trail.md)).
- **FR-10** Taxonomy management (create/rename/move/alias/merge tags, DAG edges with cycle rejection) is admin-only; regular users apply existing `active` tags.
- **FR-11** The auto-tagger maps model labels into existing `active` tags first (aliases + embedding similarity); only when nothing fits does it create a tag with status `suggested`.
- **FR-12** `suggested` tags are quarantined: visible on the file detail clearly marked as suggestions, excluded from search and autocomplete. An admin approves (→ `active`) or rejects; rejected tags are kept as soft-removed records so the same suggestion is not re-created by later runs.

## API surface

`GET/POST/DELETE /files/{id}/tags` · `POST /files/{id}/tags/{tag}/confirm` · `POST /files/{id}/tags/{tag}/reject` · `GET /tags?prefix=…` · `GET/POST/PATCH /tags` (taxonomy admin) · `POST /tags/{tag}/approve|reject` (suggestions, admin)

## Out of scope

Per-user private tag layers (explicitly decided against — tags belong to the file). Tag-based automation rules. Tag export to sidecar files (tags are app-private for now; export possible later). Folder tags — manual, self-only, same vocabulary — are specified in [F-015/FR-9](F-015-folders.md).

## Open questions

None — Q11 (vocabulary shape) was resolved by [ADR-0006](../decisions/ADR-0006-hierarchical-tags-dag.md).

## Acceptance criteria

- An auto tag `cat (0.87)` displays provenance + confidence; after user confirmation it survives a full reprocess with a new model.
- A rejected auto tag is absent after reprocessing with the same and with a newer model version.
- Bob (write permission) adds a tag to Alice's file; Alice's next fetch shows it, stamped with Bob's user id.
- Searching `tag:invoice provenance:manual|confirmed` excludes purely auto-tagged files.
- A `suggested` tag appears on its file marked as a suggestion but is absent from search results and autocomplete; after admin approval it becomes searchable.
