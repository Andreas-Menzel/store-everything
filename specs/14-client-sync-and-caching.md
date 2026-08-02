# 14 — Client Sync & Caching

**Status:** Draft
**Related ADRs:** [ADR-0007](../decisions/ADR-0007-unified-event-log.md) (event log — the invalidation source), [ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md) (crash-only, applied client-side)
**Related features:** [F-026](../features/F-026-offline-cache-and-prefetch.md) (the testable contract) · [F-012](../features/F-012-live-updates.md) · [F-019](../features/F-019-mobile-connection.md) · [F-020](../features/F-020-mobile-library.md) · [F-024](../features/F-024-offline-files-and-downloads.md)

The normative model for how **all interactive clients** — web and native — cache server state, stay fresh, work offline, and prefetch. [13-mobile-clients](13-mobile-clients.md) holds device-*specific* rules (sources, ledger, reclaim); this spec holds what every client implements identically, so web and native cannot drift apart in cache semantics. Clients are ordinary API consumers ([08](08-api-principles.md#api-first-concretely)); everything here rides the public surface.

## Two local stores, opposite rules

A device holds up to two kinds of local data. Confusing them produces either a permission leak or data loss — the boundary is load-bearing:

| | **Cache** (this spec) | **Managed offline store** ([F-024](../features/F-024-offline-files-and-downloads.md)) |
|---|---|---|
| Contains | metadata, listings, thumbnails, visited preview assets | user-initiated downloads and pinned files |
| Owner | the system — rebuildable by definition | the user — possibly the last copy in existence |
| Revocation/trash/purge | **purged from cache** (invalidation rules below) | **kept**, badged ([F-024/FR-6–7](../features/F-024-offline-files-and-downloads.md)) |
| Eviction | LRU under limits, age GC | never auto-evicted |
| Logout | web: wiped unconditionally · native: explicit choice ([F-019/FR-4](../features/F-019-mobile-connection.md)) | explicit choice, never implicit |

## Cache layers

| Layer | Content | Web | Native |
|---|---|---|---|
| **L1 — metadata** | views list + definitions, workspace list, folder entities, folder listings and view/search result pages (as fetched, per `(container, sort, filters)`), file cards/details, the persisted `/events` cursor | IndexedDB | SQLite |
| **L2 — bytes** | thumbnails, visited preview assets (never streaming/`206` responses) | Service Worker + Cache Storage | disk LRU |
| **L3 — app shell** | UI assets so the app boots offline at all | Service Worker precache + PWA manifest | the app binary (nothing to do) |

Keying rules:

- Every entry lives in a **namespace keyed by (server, account)**. Nothing is ever read across namespaces ([F-026/FR-16](../features/F-026-offline-cache-and-prefetch.md)).
- L1 metadata entries carry `fetched_at`, the response `ETag`, and a `stale` flag. L2 entries are keyed by exact URL — safe because thumbnail URLs are immutable per (file version, size) ([09](09-previews.md#thumbnails), [F-026/FR-26](../features/F-026-offline-cache-and-prefetch.md)).
- **L2 is a lookaside, never a source** (invariant I1): the UI enumerates only L1; bytes are reachable solely through URLs held in live L1 rows. Authorization correctness is therefore enforced on L1 alone — purging a metadata row makes its bytes unreachable instantly; L2 deletion is privacy hygiene on top, not access control.
- Cached trees are rooted at the caller's **visibility roots** ([07](07-identity-permissions-sharing.md#visibility-roots-what-a-grantee-sees)): a grant root is stored as a local root (no parent). If a later grant makes an ancestor readable, the roots list refreshes and the cached tree re-roots upward; revoking an outer grant purges its subtree **except** entities that are themselves roots of other grants, then refreshes the roots list.

## Rendering contract (stale-while-revalidate)

1. If a cache entry exists for a requested surface, **first paint uses it — always, without awaiting network I/O** ([F-026/FR-2](../features/F-026-offline-cache-and-prefetch.md)).
2. Revalidation runs in the background when the entry is `stale`, older than a short window (~30 s), or was never confirmed this session; conditional requests ([08 § caching](08-api-principles.md#conventions-proposed)) make the no-change case a `304`. Results patch the UI in place; stable ids keep reordering coherent.
3. Offline: cached surfaces render read-only with an offline indicator and the server-time `as of` stamp of their data; uncached targets show an explicit offline-empty state; server-requiring actions fail visibly ([F-019/FR-6](../features/F-019-mobile-connection.md) generalized to all clients). Offline sessions never mutate cache content — the cache's consistency point is the last connected moment.

## Invalidation

Doorbells are lossy by design ([12 § durable schedules, lossy doorbells](12-reliability.md#durable-schedules-lossy-doorbells)), so no case may rely on the doorbell alone. Four safety nets, decreasing in freshness:

- **N1 — doorbell** (online): an [F-012](../features/F-012-live-updates.md) notification marks the resource stale; visible surfaces refetch immediately. Container listings stay live via `folder.children_changed` ([F-012/FR-8](../features/F-012-live-updates.md)).
- **N2 — catch-up** (reconnect/app open): replay `/events` from the persisted cursor, applying invalidations in order. **The cursor advances only after the invalidations it implies are durably applied** (invariant I2 — the client-side mirror of the outbox's ack-after-persist; a crash between the two replays idempotently).
- **N3 — backstop** (lazy): any revalidation answering `401/403/404/410` purges the entry and removes it from cached containers. This net catches every case push can't reach (e.g. access lost indirectly through a move out of a granted scope — [F-015/FR-4](../features/F-015-folders.md) — where the server does not compute the loser set).
- **N4 — age GC**: entries unrendered for a bounded period are dropped regardless ([F-026/FR-18](../features/F-026-offline-cache-and-prefetch.md)).

Case matrix (triggers → cache action):

| Trigger | Via | Action |
|---|---|---|
| Logout (user action) | local | web: journaled full namespace wipe · native: explicit choice ([F-019/FR-4](../features/F-019-mobile-connection.md)); a kept cache is inert until same-account re-auth |
| Token rejected (revoked, password changed) | `401` | **lock now** (nothing renders), keep data; same-account re-auth → keep + N2; no re-auth within grace (default 72 h) → wipe |
| Account disabled/deleted | problem `type` on `401/403` ([08 § errors](08-api-principles.md#errors-rfc-9457)) | immediate wipe — remote-wipe-on-next-contact |
| Different account signs in | local | previous account's cache namespace wiped before activation (its F-024 store stays inert and hidden) |
| Server unreachable / token merely expired | — | not a security signal: keep serving (this *is* the offline feature) |
| Read revoked (file/folder/workspace) | N1 ([F-012/FR-4](../features/F-012-live-updates.md)) · N2 ([F-012/FR-9](../features/F-012-live-updates.md)) · N3 | purge entry + walk own cached subtree (L1 stores parents) + delete referenced L2 bytes; rows *removed* from cached listings, not just marked stale |
| Access lost indirectly (moved out of granted scope) | N1 on the source container · N3 | listing refresh drops the row; direct entries die by backstop |
| Grant added | N1/N2 announce | nothing purged; affected listings marked stale |
| Trash / restore / move / rename / new version | N1/N2 | entry + affected containers stale; new version → new thumbnail URL, old bytes become unreachable and age out (the previous version's cached thumbnail may render until the new one is fetchable — no grid holes) |
| Purge | N1/N2 | erase metadata **and** bytes — a purged id yields no cached artifact ([02 § invariants](02-domain-model.md#invariants)) |
| Event not mappable to concrete entries | N2 | escalate: drop L1 wholesale (self-healing default for event kinds added later) |
| Cursor expired/unknown (feed compaction, server replaced) | N2 error | drop L1, **keep L2** (identity-keyed bytes can never render wrongly under I1; stale ones LRU out), adopt new cursor, rebuild lazily |
| Storage corruption / schema mismatch | local | discard layer, rebuild — never a broken UI |

## Auth-state policy (why lock-then-wipe)

From the first rejected request the lock already denies all UI access — the grace window postpones only *disk destruction*. Wipe-on-first-401 would defeat no attacker the grace admits (a disk-level attacker is stopped by OS storage encryption and the app lock [F-019/FR-7](../features/F-019-mobile-connection.md), not by wipe timing; a stolen device kept offline never observes the rejection at all), while the common benign case — a password change fanning `401`s to every device — would force full re-warms. 72 h covers "password changed Friday, phone reopened Monday"; an evicted device that stays online destroys its cache within days; terminal account states skip the grace entirely. Defaults are starting points ([Q48](../OPEN-QUESTIONS.md)).

Crash-only mechanics (ADR-0010 device-side, extending [13 § ledger](13-mobile-clients.md#the-upload-ledger)):

- **Journaled wipes** (invariant I3): a wipe writes a *condemned-namespace* tombstone, deletes, then clears the tombstone; startup re-runs pending tombstones before serving cache — an interrupted wipe cannot leave half a cache.
- **Generation guard**: every wipe increments a namespace generation; responses from requests started under an older generation are discarded, never written — otherwise a late-arriving prefetch response silently repopulates a wiped cache.
- Staleness windows use client-monotonic time; `as of` badges display server timestamps. The client wall clock is never a correctness input.

## Bounds & eviction

The cache is bounded and disposable; the *structure and numbers* of the budgets are deliberately open ([Q49](../OPEN-QUESTIONS.md)). Fixed here:

- **Eviction rank** (first → last): purge/condemned leftovers → prefetched-but-never-rendered, oldest first (speculation pays rent or leaves) → rendered entries, LRU by **last-rendered** time (background revalidation does not count as use) → the **protected structure class**: views list + definitions, workspace list, folder entities — tiny data that guarantees the folder *shape* survives offline even after listings and bytes churn.
- **Never persisted**: credentials (platform secure store / web session mechanisms only), trash listings, audit surfaces, share-link viewer content, streaming/`206` responses, anything `Cache-Control: no-store` ([F-026/FR-19](../features/F-026-offline-cache-and-prefetch.md)).
- **Quota pressure** (web `QuotaExceededError` or platform equivalent): shrink, evict, retry once; if storage keeps failing, degrade to session-memory caching — a cache failure never takes the app down.
- Committed anchors Q49 must compose with: the native thumbnail cache default ([F-020/FR-6](../features/F-020-mobile-library.md)) and the storage-manager categories ([F-024/FR-9](../features/F-024-offline-files-and-downloads.md)).

## Prefetch

The client decides *what and when* (it alone sees viewport, intent, visibility, connection class); the server makes speculation cheap and correct (`304`s, immutable thumbnails, doorbells) and holds **no speculative per-user state** — a push channel cannot beat a local cache that is already warm, and speculative server state is exactly what ADR-0010 avoids rebuilding.

| Trigger | Prefetch | Bound |
|---|---|---|
| App open / focus | navigation (`GET /views` + roots), current folder, first page of visible views | ≤ 8 views × 1 page; views whose request contains query text are skipped (embedding cost) |
| List scroll past 75 % | next page | 1 page ahead |
| Folder listing in viewport, idle | first page of visible subfolders | ≤ 8, cancelled on navigation |
| Lightbox open | neighbors ±2: 1024-px thumbnail + preview *descriptor* | never preview media bytes |

Safety rules ([F-026/FR-24](../features/F-026-offline-cache-and-prefetch.md)): prefetch issues only reads that are already materialized server-side — never requests that enqueue generation work (PDF pages, renditions, archives are P0 jobs — [09 § generation policy](09-previews.md#generation-policy)), never original content or streaming bytes; disabled on metered connections unless opted in; `Save-Data` honored on web. Prefetched entries are indistinguishable from on-demand entries with respect to invalidation — "what if it changed in the meantime" has one answer for all cached data: render, revalidate, patch (plus keyset-stable cursors and id-dedupe at page seams — [08 § pagination](08-api-principles.md#conventions-proposed)).

## Deliberate non-mechanisms

- **No server-side materialization of view results.** [F-017 § liveness](../features/F-017-views.md#what-is-a-view--and-what-is-not) already rejects query-aware invalidation (ranked/semantic membership cannot be maintained incrementally); results are per-caller ([F-017/FR-5](../features/F-017-views.md)), so materialization would be per (view, user) and every permission or content event would have to be percolated against every stored predicate. Perceived latency is won by the client cache (0 ms first paint) — a precomputed list cannot beat it. If a specific request shape is ever proven slow at the [Q27](../OPEN-QUESTIONS.md) scale, the escape hatch is a short-TTL per-(user, view) response cache invalidated by the event stream — disposable, crash-safe, benchmark-gated, not v1.
- **No offline mutations.** Offline writes live where their ledgers live: auto-upload ([F-021](../features/F-021-mobile-auto-upload.md)), explicit downloads ([F-024](../features/F-024-offline-files-and-downloads.md)). The cache is read-only state.
- **No full-library mirror.** v1 caches what the client has seen. A complete-metadata sync client remains a deferred consumer of the same primitives — change feed + content hashes + resumable transfer ([08 § design constraints](08-api-principles.md#design-constraints-from-deferred-features)).
- **No offline search** in v1 (candidate later: name search over cached L1).

## Web platform reality

Browser storage is evictable: WebKit deletes all script-writable storage after 7 days without interaction unless the app is installed to the home screen; other engines evict under quota pressure. Clients request persistence (`navigator.storage.persist()`), but **web offline is best-effort by contract** — the native apps are the reliable offline surface, implementing this same spec on durable storage. Stance and defaults: [Q49](../OPEN-QUESTIONS.md).
