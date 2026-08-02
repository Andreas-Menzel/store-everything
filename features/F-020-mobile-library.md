# F-020 — Mobile Library: Timeline, Browse & Viewer

**Status:** Draft
**Priority:** P1
**Clients:** Android, iOS — the server capabilities this rides ([F-002/FR-19–20](F-002-hybrid-search.md), [F-017](F-017-views.md)) are client-agnostic; this file holds only the native apps' presentation and performance obligations (the web timeline rides the same server FRs — [Q26](../OPEN-QUESTIONS.md) territory)
**Depends on:** [F-002](F-002-hybrid-search.md) (search/listing, FR-19 histogram, FR-20 projection), [F-017](F-017-views.md) (views & navigation state), [F-015](F-015-folders.md) (browse), [F-012](F-012-live-updates.md) (liveness), [F-019](F-019-mobile-connection.md), [F-003](F-003-tagging.md)
**Related specs:** [06-search](../specs/06-search.md), [09-previews](../specs/09-previews.md), [13-mobile-clients](../specs/13-mobile-clients.md)

## Summary

Browsing must feel instant at 100k+ items on a phone: a date-bucketed timeline whose complete scroll geometry is known from one histogram request, a scrubber with month labels, placeholder-first grid cells that never render blank, an immutability-exploiting thumbnail cache, and a per-class viewer built on the preview descriptor ([09](../specs/09-previews.md#previews)) — including opening files *at a position* (video at 04:12, PDF at page 40). Library navigation is the server-side view set ([F-017](F-017-views.md)), so tabs, hiding, and ordering match the web account state automatically.

## User stories

- As a user, I want to fling from today to 2019 with a scrubber showing months so that finding old photos takes seconds.
- As a user, I want grids to show correctly-shaped blurred placeholders instantly so that scrolling never shows blank or jumping cells.
- As a user, I want tapping a search hit at 04:12 to open the video there so that positional search works on my phone like on the web.

## Functional requirements

- **FR-1** The timeline renders from bucket counts: one date-histogram request ([F-002/FR-19](F-002-hybrid-search.md)) fixes the complete scroll geometry (every month's extent, including the trailing undated bucket) before any item loads; jumping to any month issues at most one listing request for that bucket ([F-002/FR-20](F-002-hybrid-search.md) projection with a date-range filter).
- **FR-2** The scrubber handle shows **month + year** while dragging; releasing lands inside the labeled month (deterministic, testable by label vs. landed viewport).
- **FR-3** Every grid cell renders an aspect-correct placeholder (intrinsic dimensions + placeholder hash from the compact projection — [09](../specs/09-previews.md#thumbnails)) before its thumbnail arrives; the thumbnail swap causes no layout shift. A visible cell without at least a placeholder is a defect.
- **FR-4** *(verify: benchmark)* Scrubbing a 100k-item library end-to-end on the [Q27](../OPEN-QUESTIONS.md) reference devices: p95 frame time ≤ 17 ms with a warm thumbnail cache, ≤ 34 ms cold.
- **FR-5** *(verify: benchmark)* Cold app start to interactive timeline (placeholders rendered, scroll responsive) ≤ 2 s on reference devices with a reachable server; ≤ 3 s offline from cache.
- **FR-6** Thumbnails are cached on device keyed by (file version, size) and served from cache without revalidation requests — the URL is immutable per [09](../specs/09-previews.md#thumbnails). The cache is size-capped (default 1 GB, user-adjustable) with LRU eviction; cached regions of the library remain browsable offline ([F-019/FR-6](F-019-mobile-connection.md)).
- **FR-7** Thumbnail fetching is velocity-aware: during fast scrolling only placeholders render; fetches are issued for settled viewports plus a bounded prefetch margin, and in-flight requests for cells scrolled out of that margin are cancelled.
- **FR-8** Library navigation renders the server's navigation entries — system views, personal views, dedicated pages — with per-user `hidden`/`position` applied and editable in-app ([F-017/FR-7](F-017-views.md)); a change made on mobile is visible to the same account on the web and vice versa.
- **FR-9** Browse: the folder tree via `GET /folders/{id}/children` with cursor pagination and the server sorts; folder rows show the [F-015/FR-8](F-015-folders.md) aggregates with their `as_of` freshness.
- **FR-10** Image viewing is progressive: cached thumbnail → 1024-tier → full preview, upgraded in place without flicker; original bytes are never fetched just to display ([09](../specs/09-previews.md) previews exist for this).
- **FR-11** Video viewing plays the preview rendition as a stream (Range requests), uses the scrub sheet for seek thumbnails, and supports **open-at-timestamp**: an anchor of `t=252` starts playback within ±2 s of 04:12.
- **FR-12** Document viewing renders server-side page images on demand and supports **open-at-page**: an anchor of page 40 fetches and shows exactly page 40 first, without fetching preceding pages.
- **FR-13** Audio viewing shows the waveform and streams the file with Range support; transcript segments (where extracted) seek on tap.
- **FR-14** A file whose preview descriptor offers nothing renders type icon + metadata + download action ([F-024](F-024-offline-files-and-downloads.md)) — never a broken viewer.
- **FR-15** The file detail shows tags with provenance and confidence and supports add/remove/confirm/reject and metadata edits with `write` permission — [F-003](F-003-tagging.md) parity, same rules, stamped with the acting user.
- **FR-16** Open library screens refresh on [F-012](F-012-live-updates.md) notifications (coalesced re-execution of the visible request); reconnect catch-up uses the `/events` cursor exactly as specified there.
- **FR-17** *(negative space)* Browsing and viewing alone issue no state-changing API call: no implicit marks, no writes — the only server work a read-only session may trigger is on-demand preview generation (P0 jobs, [09](../specs/09-previews.md#generation-policy)).

## API surface

Consumes only existing/extended surface: `POST /search` (incl. [F-002/FR-19–20](F-002-hybrid-search.md)) · `GET /views` ([F-017](F-017-views.md)) · `GET /folders/{id}/children` · `GET /files/{id}` + `/thumbnail` + `/preview` + `/segments` + `/tags` · `WS /ws` + `GET /events`. Adds no server endpoints.

## Out of scope

Offline pinning and download management ([F-024](F-024-offline-files-and-downloads.md)). Search UI ([F-025](F-025-client-parity.md)). Map rendering specifics ([F-025](F-025-client-parity.md), [Q35](../OPEN-QUESTIONS.md)). Editing file content.

## Open questions

[Q27 (reference devices for mobile benchmarks)](../OPEN-QUESTIONS.md) · [Q42 (512 px thumbnail tier)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-1, FR-2) With 100k dated files, the timeline's scrollbar extent is correct immediately after one histogram response; dragging the scrubber to "May 2019" lands in May 2019 and populates it with one bucket request.
- **AC-2** (FR-3) Instrumented scroll over an uncached region: zero frames contain a cell without placeholder; thumbnail arrival changes no cell geometry.
- **AC-3** (FR-6) Re-opening a previously viewed month offline shows its thumbnails from cache; a network trace of a warm-cache session contains zero thumbnail revalidation requests.
- **AC-4** (FR-11, FR-12) Opening a search hit anchored at `t=252` starts the video within ±2 s of 04:12; opening a page-40 hit renders page 40 first and the trace shows no earlier page fetches.
- **AC-5** (FR-8) Hiding *Videos* and reordering tabs on the phone changes the web app's navigation for the same account after refresh, and vice versa.
- **AC-6** (FR-15) Confirming an auto tag on the phone survives reprocessing ([F-003/FR-4](F-003-tagging.md)) and shows the acting user's id in the web app.
- **AC-7** (FR-16) A tag edit from another client appears on the open mobile detail view in < 2 s ([F-012](F-012-live-updates.md) AC parity).
- **AC-8** (FR-17) A full browse/view session recorded at the API: zero non-GET requests except preview-generation triggers.
