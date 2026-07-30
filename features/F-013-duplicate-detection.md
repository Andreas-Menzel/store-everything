# F-013 — Duplicate Detection (Exact, v1)

**Status:** Draft
**Priority:** P1
**Depends on:** F-001 (content hashes)
**Related specs:** [02-domain-model](../specs/02-domain-model.md#fileversion)

## Summary

Exact duplicates — files with identical content hashes — are surfaced to the user: on a dedicated review page with bulk actions, and as a note on each affected file's detail view. Detection is computed at query time over the files the viewing user can read, so it can never leak the existence of other users' identical files. v1 is exact-hash only; perceptual near-duplicates (resized/re-encoded photos) are a future extractor output that slots into the same UI.

## User stories

- As a user, I want to see all duplicate files across my workspaces so that I can clean up and reclaim space.
- As a user viewing a file, I want to see that identical copies exist (that I can access) so that I notice duplication in context.
- As a user, I want bulk resolution ("keep oldest, trash the rest") so that cleaning 500 duplicate photos isn't 500 clicks.

## Functional requirements

- **FR-1** A duplicate group = files whose *latest versions* share a content hash, computed at query time over files the caller can read — never from a precomputed instance-wide table (permission leak).
- **FR-2** Duplicates page with a **scope filter**: own workspaces (default) / everything readable / specific workspaces. The chosen scope is persisted as a user preference — no separate settings screen.
- **FR-3** File detail includes the file's visible duplicates (cheap indexed hash lookup).
- **FR-4** Resolution: per group, pick the keeper; bulk rules over selected groups (keep oldest / newest / the copy in workspace X). Deletions go to **trash**, never hard delete; every action is audited ([F-011](F-011-audit-trail.md)).
- **FR-5** Copies the user can see but not delete (shared read-only) are displayed, marked non-actionable, with the reason stated inline.
- **FR-6** Groups can be marked *ignored* (per user) to declutter the default view; ignored groups remain retrievable via filter.
- **FR-7** Cross-owner groups are grouped by ownership and never auto-resolved — resolving those is human coordination, not a bulk action.

## API surface

`GET /duplicates?scope=…` (groups + cursor) · `POST /duplicates/resolve` (bulk: keeper strategy + group ids) · duplicates field on `GET /files/{id}`

## Out of scope

Perceptual near-duplicates (pHash / CLIP distance — future extractor output feeding this same feature). Automatic resolution without user action. Hardlinking/dedup at the storage layer (conflicts with portability). Duplicates among *old* versions.

## Open questions

None currently.

## Acceptance criteria

- Two byte-identical files in a user's two workspaces form one group; "keep oldest" trashes the newer one and writes an audit record.
- Bob's private file identical to Alice's never appears in Alice's duplicates view or counts.
- An ignored group disappears from the default listing and returns via the `ignored` filter.
- A read-only shared copy in a group is visibly non-actionable and survives bulk resolution.
