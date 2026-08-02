# F-026 — Offline Cache & Prefetch

**Status:** Draft
**Priority:** P1
**Clients:** all — web offline is best-effort under browser storage eviction ([14 § web platform reality](../specs/14-client-sync-and-caching.md#web-platform-reality)); native apps are the reliable offline surface
**Depends on:** [F-012](F-012-live-updates.md) (doorbell + `/events` catch-up), [F-002](F-002-hybrid-search.md) (listings, compact projection), [F-015](F-015-folders.md) (folder listings), [F-017](F-017-views.md) (views as navigation), [F-019](F-019-mobile-connection.md) (device sessions, logout choice)
**Related specs:** [14-client-sync-and-caching](../specs/14-client-sync-and-caching.md) (the governing contract), [13-mobile-clients](../specs/13-mobile-clients.md#caching-downloads-integrity), [08-api-principles](../specs/08-api-principles.md#conventions-proposed), [09-previews](../specs/09-previews.md#thumbnails)

## Summary

Clients keep a local, disposable **cache** of what they have seen — navigation, folder structure, listings, file cards, thumbnails — so previously visited surfaces render instantly (stale-while-revalidate) and remain readable offline, and they **prefetch** what the user is about to need (visible views on open, next pages, viewport subfolders, lightbox neighbors). Freshness rides the machinery that already exists: [F-012](F-012-live-updates.md) doorbells invalidate while online, the `/events` cursor feed reconciles after offline, and `401/403/404/410` on revalidation is the backstop. The cache is the opposite of the managed offline store ([F-024](F-024-offline-files-and-downloads.md)): system-owned, rebuildable, and purged on revocation — never a place user data can be lost, never a place revoked data can hide. Semantics are identical on web and native ([14](../specs/14-client-sync-and-caching.md)); server-side this feature adds only conditional requests and version-pinned thumbnail URLs — no per-user speculative state, no materialized view results ([14 § deliberate non-mechanisms](../specs/14-client-sync-and-caching.md#deliberate-non-mechanisms)).

## User stories

- As a user opening the app in a dead spot, I want the folders and thumbnails I've browsed before to still be there, clearly marked as offline, so that the app is useful without a connection.
- As a user clicking through my library tabs, I want each page to appear instantly with what was there a moment ago and quietly update, so that navigation never feels like waiting.
- As a user scrolling a big folder, I want the next page to already be there when I reach it.
- As an owner revoking someone's access, I want their device to stop showing my files the next time it talks to the server — cached or not.
- As a user logging out on a shared computer, I want nothing of my library left behind in the browser.

## Functional requirements

Cache & offline reads:

- **FR-1** With the server unreachable, previously rendered surfaces (folder listings, view results, file details) and previously fetched thumbnails/preview assets render from local cache, read-only, each surface showing an offline indicator and the server-time `as of` stamp of its data; never-cached targets show an explicit offline-empty state. (Extends [F-019/FR-6](F-019-mobile-connection.md) to all clients.)
- **FR-2** *(verify: benchmark)* When a cache entry exists for a surface, first paint is served from it without awaiting network I/O: p95 time-to-first-paint on warm start ≤ 200 ms on the [Q27](../OPEN-QUESTIONS.md) reference devices, unchanged when server RTT is artificially ≥ 5 s.
- **FR-3** *(negative space)* Cached bytes render only via a live metadata entry: content whose metadata entry was purged from the cache is not renderable through any UI path, even while its bytes remain on disk pending eviction ([14 § invariant I1](../specs/14-client-sync-and-caching.md#cache-layers)).

Freshness & invalidation:

- **FR-4** An [F-012](F-012-live-updates.md) notification for a cached resource marks it stale; surfaces currently displaying it refetch immediately (within the [F-012](F-012-live-updates.md) < 2 s bound end-to-end).
- **FR-5** *(verify: fault-injection)* On reconnect or start with connectivity, the client replays `/events` from its persisted cursor and applies the implied invalidations in order; the cursor is persisted only after those cache mutations are durable — a crash between the two replays idempotently and converges.
- **FR-6** A cursor rejected as expired/unknown discards the metadata layer wholesale (byte cache retained), adopts a fresh cursor, and rebuilds lazily — no user-visible failure beyond cold-cache refetches.
- **FR-7** *(negative space)* After catch-up completes, no resource whose read access was revoked while the client was offline renders from cache — the entry, its cached subtree (for folders/workspaces), its rows in cached listings, and its referenced byte entries are removed. Verified with [F-002/FR-7](F-002-hybrid-search.md) leak-test rigor.
- **FR-8** *(negative space)* A purge event erases the item's cached metadata and bytes; thereafter the cache yields no artifact for that id ([02 § invariants](../specs/02-domain-model.md#invariants) client-side).
- **FR-9** A revalidation answering `401/403/404/410` purges the entry and removes it from cached containers, without automatic retry of the same request.

Sessions, accounts, wipes:

- **FR-10** Cached content renders without a server-validated credential when the server is unreachable or the token is merely expired — token *expiry* alone never locks or wipes the cache; server-requiring actions fail visibly per [F-019/FR-6](F-019-mobile-connection.md).
- **FR-11** From the first response that rejects the credential (`401` with a re-authenticatable problem type), the client locks: no cached content renders until re-authentication. Re-authentication as the same account retains the cache and triggers FR-5 catch-up.
- **FR-12** Without same-account re-authentication within a grace period after rejection (default 72 h — [Q48](../OPEN-QUESTIONS.md)), the namespace is wiped.
- **FR-13** An auth failure whose problem `type` marks a terminal account state (`account_disabled` / `account_deleted` — [08 § errors](../specs/08-api-principles.md#errors-rfc-9457)) wipes the cache namespace immediately, skipping the grace period. The server distinguishes these from re-authenticatable failures in the problem `type`.
- **FR-14** Logout wipes the cache namespace on web unconditionally; on native, cache and download removal follow the explicit logout choice of [F-019/FR-4](F-019-mobile-connection.md) — a kept cache is inert (renders for no one) until the same account re-authenticates, and is wiped before a different account becomes active.
- **FR-15** *(verify: fault-injection)* Wipes are crash-safe and race-safe: an interrupted wipe completes on next start (journaled tombstone), and no response belonging to a request started before a wipe is written to the store afterwards (namespace generation guard).
- **FR-16** *(negative space)* Cache entries written under one (server, account) namespace never render under another. No cross-namespace reads exist.

Bounds & hygiene:

- **FR-17** Cache storage is bounded by enforced limits; exceeding a limit triggers eviction in the [14 § eviction rank](../specs/14-client-sync-and-caching.md#bounds--eviction) order (prefetched-never-rendered first, then LRU by last-rendered time, protected structure class last). Budget structure and defaults: [Q49](../OPEN-QUESTIONS.md).
- **FR-18** Entries not rendered for a bounded period (default 90 d — [Q48](../OPEN-QUESTIONS.md)) are removed by a periodic local sweep independent of budget pressure.
- **FR-19** *(negative space)* Never persisted in the cache: credentials (platform secure store / web session mechanisms only — [F-019/FR-5](F-019-mobile-connection.md)), trash listings, audit surfaces, share-link viewer content, streaming/`206` responses, and any response marked `Cache-Control: no-store`. Verified by storage inspection after exercising each surface.
- **FR-20** *(verify: fault-injection)* A corrupted or schema-mismatched cache store is discarded and rebuilt on next start; the app remains functional with no user-visible failure beyond cold-cache behavior.

Prefetch:

- **FR-21** On app open/focus the client refreshes navigation and prefetches the current folder plus the first page of visible (non-hidden) views — at most 8 views, skipping views whose stored request contains query text; after warm-up, activating a warmed view paints from cache per FR-2.
- **FR-22** While browsing: the next listing page is prefetched when scrolling passes 75 %; first pages of at most 8 subfolders visible in the viewport are prefetched during idle (cancelled on navigation); an open lightbox prefetches neighbors ±2 as 1024-px thumbnail + preview descriptor only.
- **FR-23** Prefetched entries obey the same staleness, invalidation, and eviction rules as on-demand entries; activating a surface whose prefetched data is stale triggers immediate revalidation (FR-4 path).
- **FR-24** *(negative space)* Prefetch never issues a request that can enqueue server-side generation work (uncached PDF pages, renditions, archives — [09 § generation policy](../specs/09-previews.md#generation-policy)), never fetches original file content or streaming preview bytes, and is disabled on metered connections unless the user opts in (`Save-Data` honored on web).

Server contract:

- **FR-25** Cacheable JSON reads (folder metadata and children, file metadata, views) return a strong `ETag`; a request whose `If-None-Match` matches is answered `304` with an empty body ([08 § caching](../specs/08-api-principles.md#conventions-proposed)).
- **FR-26** `GET /files/{id}/thumbnail?size=N&v={version}` returns that version's thumbnail — any existing version of a readable file ([F-007](F-007-versioning.md)) — with `Cache-Control: private, max-age=31536000, immutable`; without `v` the current version is served without the immutable marker. Listing rows carry the current version id ([F-002/FR-20](F-002-hybrid-search.md)) so clients can construct version-pinned URLs.

## API surface

No new endpoints. Consumes `GET /views`, `GET /folders/{id}` + `/children`, `GET /files/{id}` + `/thumbnail` + `/preview`, `POST /search`, `WS /ws` + `GET /events` ([F-012](F-012-live-updates.md)). Adds server behavior only: `ETag`/`If-None-Match`/`304` on cacheable reads (FR-25), the `v` thumbnail parameter with immutable caching (FR-26), and terminal-state auth problem types (FR-13) — conventions recorded in [08](../specs/08-api-principles.md#conventions-proposed).

## Out of scope

Offline mutations and upload queues ([F-021](F-021-mobile-auto-upload.md) ledger, [F-024](F-024-offline-files-and-downloads.md) store). The managed offline store itself ([F-024](F-024-offline-files-and-downloads.md) — opposite lifecycle rules). Full-library metadata mirroring (a future sync client — [08 § design constraints](../specs/08-api-principles.md#design-constraints-from-deferred-features)). Offline search over cached metadata. Share-link viewer caching (`no-store`, FR-19).

## Open questions

[Q48 (retention defaults: rejection grace, age GC, offline TTL)](../OPEN-QUESTIONS.md) · [Q49 (cache budget structure)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-1, FR-3) Browse three folders and open a view online, then go offline (airplane mode / network cut): all four surfaces render with offline indicator and `as of` stamps; their thumbnails display; a never-visited folder shows the offline-empty state; no mutation control is operable.
- **AC-2** (FR-2) With cache warm and server latency forced to 5 s, navigating to a cached folder paints its cached listing in ≤ 200 ms (p95, reference devices); the delayed response then updates the view in place.
- **AC-3** (FR-4) Two clients on the same folder: client A renames a file; client B's open listing shows the change in < 2 s without user action.
- **AC-4** (FR-5, FR-7) Client goes offline; server-side: 100 changes including revocation of one cached folder subtree and one cached file. On reconnect the client catches up exactly ([F-012](F-012-live-updates.md) AC), after which neither revoked item renders anywhere — listings, detail, search-result cache, bytes — verified at leak-test rigor.
- **AC-5** (FR-5, FR-15) Kill the client process between applying a replayed revocation and persisting the cursor; on restart, replay re-applies the purge and the cursor advances — the revoked entry is gone. Kill mid-logout-wipe: on restart the namespace completes wiping before anything renders. A prefetch response arriving after the wipe is not stored.
- **AC-6** (FR-11, FR-12, FR-13) Revoke the device's token server-side: the client locks on its next request (nothing cached renders). Re-login as the same account → cached surfaces return, then reconcile. Repeat with no re-login and the clock advanced past the grace period → namespace wiped. Repeat with the account deleted → wipe occurs on first contact, no grace.
- **AC-7** (FR-14, FR-16) Web: logout, then inspect origin storage — no metadata, bytes, or cursor remain. Native: log out choosing "keep", sign in as a second account — nothing of account A renders and A's cache namespace is gone; A's downloads are not silently deleted ([F-024/FR-7](F-024-offline-files-and-downloads.md)).
- **AC-8** (FR-21, FR-22, FR-24) Scripted session (app open with 6 visible views incl. one semantic view → scroll a 500-item folder to 80 % → open lightbox, step twice) under a recorded network trace: exactly the FR-21/FR-22 request set — no semantic-view execution, ≤ 8 view first-pages, one next-page fetch, neighbor thumbnails/descriptors only; zero requests to generation-triggering endpoints, zero original-content or preview-media bytes; repeat on a metered profile with prefetch off → only on-demand requests remain.
- **AC-9** (FR-25) `GET /folders/{id}/children`, then repeat with `If-None-Match`: `304`, empty body. Add a file, repeat: `200`, new `ETag`, new listing.
- **AC-10** (FR-26) Fetch a thumbnail with `v={current}`: response carries the immutable `Cache-Control`. Upload a new version: listing rows carry the new version id; the old `v` URL still serves the old version's thumbnail; the new URL serves the new one; a warm client session revalidates zero thumbnails ([F-020](F-020-mobile-library.md) AC-3 remains satisfied).
- **AC-11** (FR-19) Visit trash, audit (as admin), and a share link, then inspect persistent storage: none of their responses are present.
- **AC-12** (FR-20) Corrupt the metadata store on disk; next start rebuilds silently: surfaces load from network, no error dialog, and the corrupted store is replaced.
