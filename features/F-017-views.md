# F-017 — Views (Saved Searches & Library Pages)

**Status:** Draft
**Priority:** P1
**Depends on:** [F-002](F-002-hybrid-search.md) (a view stores a search request), [F-015](F-015-folders.md) (folder results navigate into browse), [F-005](F-005-image-analysis.md) (`gps` metadata feeds the map)
**Related specs:** [06-search](../specs/06-search.md#views-stored-requests), [02-domain-model](../specs/02-domain-model.md#view), [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md#2-identification) (media class), [08-api-principles](../specs/08-api-principles.md)

## Summary

A **view** is a named, stored search request plus presentation hints (layout, navigation position), surfaced as a page. One mechanism serves both user-created pages ("images that contain a person") and the built-in library tabs — **Images, Videos, Audio, Documents, Map, Recent** — which are *system views* seeded at install and managed by admins. A view is a stored *query*, never a snapshot: execution is an ordinary `POST /search` under the caller's permissions, so results are always current and a view can never leak or grant anything. Type pages ride the core-assigned media `class` ([F-002/FR-15](F-002-hybrid-search.md), [04](../specs/04-ingestion-pipeline.md#2-identification)); the map page rides geo filters and grid aggregation ([F-002/FR-17, FR-18](F-002-hybrid-search.md)).

## What is a view — and what is not

A page is a view iff (1) its rows are search results (files, or folders-as-matches), (2) its predicate runs over search's universe — permission-filtered, version- and lifecycle-scoped ([F-002](F-002-hybrid-search.md)) — and (3) it needs no row fields or actions beyond what any search result supports. Consequences:

| Page | Mechanism | Why |
|---|---|---|
| Images · Videos · Audio · Documents · Map · Recent | system views | plain stored search requests |
| user-created pages | personal views | same |
| Browse | dedicated page ([F-015](F-015-folders.md)) | rows are *locations*: one tree level including empty folders, aggregates, structure operations; parameterized by the folder you stand in, not stored |
| Trash | dedicated page ([F-014/FR-3](F-014-deletion-and-trash.md)) | rows are trash *entries* (origin, batch id, purge deadline, restorability) with restore/purge actions; admins see aggregates only. Content search over trash is not this page — it already exists as the opt-in `state: trashed` scope ([F-002/FR-13](F-002-hybrid-search.md)) |
| Duplicates | dedicated page ([F-013](F-013-duplicate-detection.md)) | rows are hash *groups* with bulk-resolution actions |

**Navigation contract:** a folder-typed hit ([F-002/FR-14](F-002-hybrid-search.md)) in any view navigates to browse at that folder; a file hit opens the file at the matched position where an anchor exists.

**Liveness:** there is no per-view push channel. A client keeps an open view fresh by re-executing its request when [F-012](F-012-live-updates.md) notifications arrive (coalesced doorbell → refetch — the lossy-wakeup pattern of [12](../specs/12-reliability.md)). Server-side query-aware invalidation is deliberately absent from v1: membership in ranked/semantic results cannot be evaluated incrementally per event (any ingestion can displace results), so honest liveness degenerates to re-running the query — which the client already does. Standing-query alerts are [Q37](../OPEN-QUESTIONS.md).

## User stories

- As a user, I want an Images page listing every image I may read, newest first, so that I browse my library without composing a query.
- As a user, I want to save a search ("images containing a person") as my own page so that a recurring search is one click.
- As a user, I want a map showing every file with known coordinates so that I can find files by where they were taken.
- As a user, I want to hide tabs I never use and reorder the rest so that navigation matches my usage.
- As an admin, I want to adjust the instance's default pages so that the library fits what my users store.

## Functional requirements

- **FR-1** A user can create, list, read, update, and delete their own views. A view carries `name` (1–120 characters), `request` (a search request), `layout` ∈ `grid | list | map | timeline`, and per-user navigation state (FR-7). Personal views are visible and mutable only to their owner.
- **FR-2** The stored `request` is validated at create/update against the same schema as `POST /search` (unknown fields rejected — [08](../specs/08-api-principles.md#errors-rfc-9457)); rejection is an RFC 9457 problem listing every invalid clause with a JSON Pointer.
- **FR-3** A view that validates is executable verbatim: `GET /views/{id}` returns `request` exactly as stored, and submitting it to `POST /search` requires no transformation. Any request `POST /search` accepts — including opt-in version and lifecycle scopes — is storable.
- **FR-4** Views store configuration, never results: reading or listing views executes no search, and results exist only through `POST /search` at request time. A matching file that is trashed, or whose read grant is revoked, after the view was saved does not appear in the next execution.
- **FR-5** *(negative space)* Views confer no access: creating, possessing, or executing any view — including system views — never yields a result, facet, count, or map cell the same caller would not receive from `POST /search` directly ([F-002/FR-7](F-002-hybrid-search.md)). Two callers executing the same system view each receive exactly their own permitted results.
- **FR-6** **System views** are seeded at first startup from the default set below. Admins may add, edit, hide instance-wide, and restore-to-default; members can neither modify nor delete them (only FR-7 state). Re-seeding on upgrade restores missing defaults but never overwrites an admin-modified one. The stored request of a system view is readable by every member — an admin-authored query is instance-public by design.
- **FR-7** **Per-user navigation state:** every navigation entry — views *and* the dedicated pages (browse, trash, duplicates) — carries per-user `hidden` and `position`, persisted server-side (the [F-013/FR-2](F-013-duplicate-detection.md) preference pattern: no separate settings screen). Changing them alters no other user's navigation, and hiding an entry changes nothing about what any request may query.
- **FR-8** A stored request that no longer validates (e.g. it filters on a since-purged workspace) fails execution with the FR-2 problem shape naming the offending clause — clauses are never silently dropped. `GET /views` marks such views `valid: false`.
- **FR-9** `layout` is persisted and returned verbatim and never alters search semantics: the same `request` yields the same `POST /search` response regardless of layout. The map page's geo scoping lives in its *request* (a bounding-box predicate), not in the layout value.

### Default system views (FR-6)

| Name | Request | Sort | Layout |
|---|---|---|---|
| Images | `class = image` | `taken_at` desc (missing → last) | grid |
| Videos | `class = video` | `mtime` desc | grid |
| Audio | `class = audio` | `mtime` desc | list |
| Documents | `class = document` | `mtime` desc | list |
| Map | `gps` within world bounds + grid aggregation (client pans/zooms the bbox) | — | map |
| Recent | no filter | `ingested` desc | list |

Exact naming/composition of this set is product tuning at release; the *mechanism* (seeded, admin-owned, per-user hideable) is the requirement.

## API surface

```
GET    /views          navigation entries: system views + own views + reserved entries for the
                       dedicated pages (browse, trash, duplicates), per-user state applied, validity flags
POST   /views          create personal view
GET    /views/{id}     definition incl. stored request (verbatim)
PATCH  /views/{id}     own view: any field · system view / reserved entry: members set only
                       hidden/position (their own state), admins edit system-view definitions
DELETE /views/{id}     own views only
```

Execution has no endpoint of its own: clients run the stored request through `POST /search` ([F-002](F-002-hybrid-search.md)) — one execution path, no result state to snapshot, and interactive layouts (map pan/zoom) compose their bbox onto the stored request client-side.

## Out of scope

- Sharing personal views between users — system views are the only instance-shared ones in v1.
- Automatic result counts in the view list (N searches per navigation render; counts come from executing the view — totals and facets).
- Per-view push invalidation and standing-query alerts (client doorbell via [F-012](F-012-live-updates.md) in v1; alerts → [Q37](../OPEN-QUESTIONS.md)).
- Static hand-picked collections/albums (→ [Q36](../OPEN-QUESTIONS.md); the admin-governed tag DAG is a shared vocabulary, not personal grouping).
- Query-builder UI specifics — a client concern; the contract is `POST /search`'s schema.

## Open questions

[Q35 (map tile sourcing)](../OPEN-QUESTIONS.md) · [Q36 (static collections/albums)](../OPEN-QUESTIONS.md) · [Q37 (saved-search subscriptions)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-1, FR-3, FR-4) Creating `{name: "Dog photos", request: {query: "dog at the beach", filters: {class: "image"}}, layout: "grid"}` succeeds; `GET /views/{id}` returns the request exactly as submitted; `POST /search` accepts it unmodified. Trashing a matching photo removes it from the next execution.
- **AC-2** (FR-5) Users A and B execute the system view *Documents*: each receives only files they may read; B gets zero results, facets, and counts from A's private workspace — tested with the [F-002/FR-7](F-002-hybrid-search.md) leak-test rigor.
- **AC-3** (FR-6) A fresh instance has exactly the six defaults. An admin renames *Images* to *Bilder*; a later upgrade re-seed keeps *Bilder* and restores any missing default. A member's `PATCH` of a system view's `request` returns `403`.
- **AC-4** (FR-7) User A hides *Videos* and moves *Map* to the first position; user B's `GET /views` is unchanged; A's reflects both changes.
- **AC-5** (FR-2) Creating a view whose request contains an unknown field returns `422` with a JSON Pointer to the field; all invalid clauses are reported in one response.
- **AC-6** (FR-8) A view filtering on workspace X keeps validating while X is trashed; after X is purged, execution returns the problem shape pointing at the workspace clause and `GET /views` shows the view `valid: false`.
- **AC-7** (FR-9) Switching *Dog photos* from `grid` to `map` changes no byte of the `POST /search` response for its stored request.
- **AC-8** (FR-6; executes [F-002/FR-17, FR-18](F-002-hybrid-search.md)) The *Map* view with a bbox around Hamburg returns grid cells whose counts include only readable, live files; zoomed below the cell threshold it returns the individual geotagged files; a photo the caller cannot read appears in no cell and no count.
