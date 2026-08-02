# 13 — Mobile Clients

**Status:** Draft
**Related ADRs:** [ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md), [ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md)
**Related features:** [F-019](../features/F-019-mobile-connection.md) · [F-020](../features/F-020-mobile-library.md) · [F-021](../features/F-021-mobile-auto-upload.md) · [F-022](../features/F-022-device-storage-reclaim.md) · [F-023](../features/F-023-os-file-manager-integration.md) · [F-024](../features/F-024-offline-files-and-downloads.md) · [F-025](../features/F-025-client-parity.md)

The normative model shared by the native Android/iOS apps. The apps are **ordinary API consumers** ([08 § API-first](08-api-principles.md#api-first-concretely)) — this spec adds no server privileges; it fixes the device-side rules that the mobile features build on, and records the platform constraints those rules answer to.

## The one-way principle

The device is a **source** (backup flows device → cloud) and a **reader** (downloads are user-initiated copies cloud → device). It is never a mirror:

| Flow | Allowed | Never |
|---|---|---|
| Auto-upload ([F-021](../features/F-021-mobile-auto-upload.md)) | read device sources, upload bytes, stamp metadata at upload | modify or delete any source file |
| Reclaim ([F-022](../features/F-022-device-storage-reclaim.md)) | delete *verified* device copies through the OS-mediated, user-confirmed flow | delete anything server-side; delete without OS confirmation |
| Downloads ([F-024](../features/F-024-offline-files-and-downloads.md)) | copy server content to app-managed storage, refresh pinned copies | delete a local copy in response to *any* server event (trash, purge, revocation) |
| Server events | update app UI/state, refresh pinned downloads | reach into device sources or exports in any way |

Device-side renames, moves, and deletions after upload have **no server effect** — the backup preserves content, it does not mirror a filesystem. Explicit file management against the server happens through the normal API surfaces (in-app actions, [F-023](../features/F-023-os-file-manager-integration.md)) and is a deliberate user operation, not sync.

## Sources

A **source** is a device-side origin enrolled for auto-upload, with per-source configuration ([F-021/FR-5](../features/F-021-mobile-auto-upload.md)). Source types per platform:

| Type | Android | iOS | New-item detection guarantee |
|---|---|---|---|
| Media folders | MediaStore buckets (all volumes incl. SD; `.nomedia` trees excluded by the OS) | — (no filesystem folders for photos) | content trigger + periodic scan |
| Photo library / albums | — (folders *are* the model) | whole library (default offer) + album include/exclude + smart albums | iOS ≥ 26.1: system upload extension; older: on-open + opportunistic |
| Arbitrary folders | SAF folder grants (OS forbids granting storage/SD/Download roots — subfolders work) | Files-app folders via persistent security-scoped bookmarks | **weaker: scanned when the app runs** — labeled so in the UI |

iOS albums are labels, not partitions: an asset may be in zero or many. "All photos" must therefore be the default offer (photos in no album must not silently miss backup), and an item matched by several enabled sources uploads once, to the highest-priority source's destination ([F-021/FR-7](../features/F-021-mobile-auto-upload.md)).

## The upload ledger

Each client keeps a durable, local **ledger**: one record per (source item), tracking identity (platform asset id + `PHCloudIdentifier` where available), content hash, size/mtime as of hashing, mapped server file id, and state:

```
discovered → hashed → queued → uploading → uploaded → verified → reclaim-eligible → reclaimed
                                    ↘ blocked (missing permission / policy)   ↘ failed (cause recorded)
```

- **The client is crash-only, like the server** (ADR-0010 applied device-side): ledger transitions are durable before their side effects; the app may be killed at any instant and converges on restart. Server-side idempotency (upload sessions, `Idempotency-Key` — [12](12-reliability.md#client-visible-idempotency)) plus hash dedupe guarantee convergence without duplicates or loss.
- **`verified`** means: the server has confirmed — by content-hash equality — that a **live** file version with these exact bytes exists in a workspace the **user owns**. Upload-finalize hash verification ([03 § uploads](03-storage-and-portability.md#uploads)) sets it; the batch hash-check ([F-021 § API](../features/F-021-mobile-auto-upload.md#api-surface)) rebuilds it after reinstall or on a second device. A trashed match is **not** verified.
- **Grouped assets verify as a unit:** a paired capture (below) is `verified` only when every component is.
- **Local modification resets verification:** changed bytes are a new ledger cycle (and upload as a new *version* of the mapped file — [F-021/FR-14](../features/F-021-mobile-auto-upload.md), [F-007](../features/F-007-versioning.md)).

## Reclaim gates

[F-022](../features/F-022-device-storage-reclaim.md) may delete a device copy only when **all** of these hold, evaluated at action time — never from stale flags:

1. Ledger state `verified`, all group components included.
2. **Fresh server re-confirmation**: the mapped file (or an owned live file with the hash) still exists, live, owned.
3. **Local re-check**: size + mtime match the ledger's hashed state (else re-hash; changed bytes are never covered by old verification).
4. Policy: minimum age since verification, favorites/source exclusions ([F-022/FR-1](../features/F-022-device-storage-reclaim.md)).
5. **OS-mediated, user-confirmed deletion into the OS trash** — no other deletion path exists.

Platform deletion facts the gates answer to:

| | Android | iOS |
|---|---|---|
| Mechanism | `MediaStore.createTrashRequest` (per-batch system dialog) | `PHAssetChangeRequest.deleteAssets` (per-batch system dialog) |
| OS safety net | system trash, ~30 days | Recently Deleted, 30 days |
| Batch bound | 2 000 URIs per request (platform cap) | no documented cap (large batches proven in practice) |
| Silent path | `MANAGE_MEDIA` special permission (opt-in) — Q43 | none exists |
| Honesty items | SAF-source files are plain deletes to the OS trash equivalent where the volume provides one | space frees only when Recently Deleted empties (no API for that); with **iCloud Photos on, deletion propagates to iCloud** — flow must require explicit acknowledgment |

## Paired captures (asset groups)

Platform-agnostic mechanism for multi-file captures — Apple Live Photos (photo + paired video, two files) and RAW+JPEG (two files, both platforms). Android Motion Photos are **one** file (video embedded in the image) and need no grouping — uploaded byte-exact they are fully preserved; playback of the embedded video is extractor territory ([ADR-0008](../decisions/ADR-0008-renditions.md)).

- Groups are expressed as **well-known metadata keys** ([02 § MetadataEntry](02-domain-model.md#metadataentry)): `asset_group` (group id), `group_role` (`primary` | `motion-video` | `raw`), `group_kind` (`motion-pair` | `raw-jpeg`).
- **Any writer may stamp them**: the uploading client (authoritative — it sees PhotoKit/MediaStore truth), and later a pairing extractor for content that arrived via web upload or directly on the NAS. The mechanism carries no platform assumption.
- **On disk: always the real component files, side by side** (portability — ADR-0003 untouched). The group exists only in the organization layer.
- **Client UI contract** (all clients, web included): a group renders as **one item** — the `primary` component's tile, badged; secondary components play/open from the viewer and are offered at download. Default library views exclude non-primary components (a stored-request predicate on the system views — [F-017](../features/F-017-views.md) machinery, no new mechanism).

## Caching, downloads, integrity

The cache/offline/prefetch contract shared by *all* clients — layers, stale-while-revalidate rendering, invalidation via doorbell + `/events` catch-up, auth-state policy, eviction — is [14-client-sync-and-caching](14-client-sync-and-caching.md) ([F-026](../features/F-026-offline-cache-and-prefetch.md)); this section holds the device-side rules on top of it.

- **Thumbnails are immutable per (file version, size)** ([09](09-previews.md#thumbnails)) — clients cache them indefinitely with no revalidation, under a size-capped LRU. Grid rendering never blocks on thumbnails: listings carry dimensions + a placeholder hash ([F-002/FR-20](../features/F-002-hybrid-search.md), [09](09-previews.md#thumbnails)).
- **Downloads are hash-verified** against the version's content hash before being marked downloaded; refreshes replace bytes atomically (verify new, then swap). Pinned items are never auto-evicted; caches are.
- **Read-only hand-out (v1):** files handed to other apps (open-with/share) are read-only — a third-party app can never silently mutate the app-managed copy. Write-back into the cloud is [F-023](../features/F-023-os-file-manager-integration.md)'s job, where a save becomes an ordinary new version.

## Platform background-execution matrix

What each platform honestly supports for "upload soon after capture" — feature FRs must not promise beyond this ([F-021/FR-17–18](../features/F-021-mobile-auto-upload.md)):

| | Android | iOS ≥ 26.1 | iOS < 26.1 |
|---|---|---|---|
| Trigger on new media | MediaStore content trigger (one-shot, re-registered) + periodic catch-up scan | system-managed `PHBackgroundResourceUploadExtension`: the OS runs photo uploads with the app closed/locked | none while suspended; on-open catch-up via PhotoKit persistent change history + opportunistic `BGTask` |
| Bulk transfer | foreground service (`dataSync`) / user-initiated transfer job | system extension + in-app foreground path | in-app foreground + background `URLSession` continuation |
| Known hostile factor | OEM battery killers — the app must surface exemption status and a manual re-scan ([F-021/FR-18](../features/F-021-mobile-auto-upload.md)) | extension scheduling is still OS-paced | expectation-setting is mandatory UI, not documentation |

Byte-exactness requirements: Android needs `ACCESS_MEDIA_LOCATION` + `MediaStore.setRequireOriginal` (otherwise the OS strips GPS EXIF — silently different bytes); iOS originals come from `PHAssetResource` (never the rendered-image APIs), with iCloud-optimized originals fetched on demand.

**Server coupling:** the iOS upload extension drives the server directly using the IETF resumable-upload protocol — whether our upload wire format ([F-001/FR-2](../features/F-001-upload-and-import.md), [03](03-storage-and-portability.md#uploads)) adopts it is **Q38**, to be decided before F-001 is implemented.

## Security

Device sessions are named, scoped personal access tokens ([07](07-identity-permissions-sharing.md#tokens--credentials)), stored only in the platform secure store ([F-019/FR-5](../features/F-019-mobile-connection.md)). One-time pairing codes ([F-019/FR-3](../features/F-019-mobile-connection.md)) are credential-grade: high-entropy, short-lived, single-use, aggressively rate-limited and audited. TLS trust policy for self-signed home setups is Q40.
