# F-024 — Offline Files & Downloads (In-App)

**Status:** Draft
**Priority:** P1
**Clients:** Android, iOS — native-only for the *managed* offline store (pinning, freshness, eviction, integrity); a browser's own download function is the web equivalent and is deliberately unmanaged
**Depends on:** [F-019](F-019-mobile-connection.md), [F-016](F-016-archive-download.md) (bulk), [F-007](F-007-versioning.md), [F-014](F-014-deletion-and-trash.md) (restore), [F-012](F-012-live-updates.md) (change signals)
**Related specs:** [13-mobile-clients](../specs/13-mobile-clients.md#caching-downloads-integrity)

## Summary

Any readable file can be brought onto the device and opened — one-off downloads and **pinned** items ("keep on device") that stay current automatically. Every surface shows per-file download state. The governing rule ([13 § one-way](../specs/13-mobile-clients.md#the-one-way-principle)): a local copy is the user's — **no server event ever deletes it**. When the server file is trashed, purged, or access is revoked, the local copy stays, clearly badged, with the honest actions (restore from server trash, export, re-upload). A downloaded copy may be the last copy in existence; treating it as expendable cache would be data loss by design.

## User stories

- As a user, I want to mark a folder "keep on device" so that its current contents are always available offline.
- As a user, I want to see at a glance which files are on my phone so that I know what works on the plane.
- As a user whose file was deleted on the server, I want my downloaded copy kept and flagged so that a server-side mistake never destroys my last copy.

## Functional requirements

- **FR-1** Download targets: single file, multi-selection, or folder. Selections/folders above a threshold (default 50 files) may be fetched as one archive ([F-016](F-016-archive-download.md)) and unpacked locally; either path yields identical per-file results.
- **FR-2** **Pinning** ("keep on device") on files and folders: pinned content is kept current — a new server version triggers re-download under the configured policy (Wi-Fi default, cellular opt-in), driven by [F-012](F-012-live-updates.md) notifications with `/events` catch-up on reconnect. A pinned folder covers files later added to it.
- **FR-3** Every listing surface (timeline, browse, search results, detail) shows the item's local state: none · downloading (progress) · downloaded · update-downloading · error. State is consistent across surfaces for the same file.
- **FR-4** A download is marked complete only after its bytes hash-match the version's content hash; refreshes verify the new bytes **before** atomically replacing the old ([13 § integrity](../specs/13-mobile-clients.md#caching-downloads-integrity)) — a torn or corrupt local file is impossible by construction, and a mismatch surfaces as a retryable error.
- **FR-5** Server-side rename/move updates the local item's displayed path with no re-download (identity = file UUID + version).
- **FR-6** Server-side divergence states, each badged on the item and collected in a **"Removed from server"** section of the storage manager:
  - file **trashed** → keep copy; badge "in server trash, restorable until {deadline}"; offer one-tap restore ([F-014/FR-4](F-014-deletion-and-trash.md));
  - file **purged/absent** → keep copy; badge "no longer on server — this device holds the only copy"; offer export and re-upload;
  - **access revoked** → keep copy; badge "access removed".
- **FR-7** *(negative space)* No server event — trash, purge, revocation, version change — ever deletes or modifies a local copy without an explicit local user action. (Revocation therefore does not reach into devices; the spec says so rather than implying remote wipe exists.)
- **FR-8** Open-with/share hands other apps a **read-only** copy; export ("save to device/gallery") is an explicit copy-out. *(negative space)* No third-party app can mutate the app-managed copy in place; write-back into the cloud is [F-023](F-023-os-file-manager-integration.md)'s versioned path.
- **FR-9** Storage manager: usage by category (pinned · downloads · thumbnail cache · preview cache), per-item and per-category eviction, and cache caps. *(negative space)* Pinned items and "Removed from server" items are never auto-evicted; only caches are LRU-evicted.
- **FR-10** Downloaded and pinned content opens in the [F-020](F-020-mobile-library.md) viewer offline, including positional opening where the needed assets are local.

## API surface

Adds no endpoints. Consumes `GET /files/{id}/content` (Range), `GET /files/{id}/versions`, `POST /archives` ([F-016](F-016-archive-download.md)), `POST /files/{id}/restore` ([F-014](F-014-deletion-and-trash.md)), `WS /ws` + `GET /events`.

## Out of scope

The OS-file-surface representation of downloads ([F-023](F-023-os-file-manager-integration.md)). Editing local copies with write-back. Peer-to-peer/offline sharing.

## Open questions

None feature-local.

## Acceptance criteria

- **AC-1** (FR-2) Pinning a 500-file folder downloads all current files; adding a file server-side gets it pinned and downloaded; uploading a new version of one file replaces the local copy only after hash verification, and the previous version remains restorable server-side ([F-007](F-007-versioning.md)).
- **AC-2** (FR-4) Injecting corruption into a transfer leaves the item in `error` with the old copy intact; no partially-written file is ever observable at the storage path.
- **AC-3** (FR-6) Trashing a downloaded file server-side: the local copy stays, badged with the purge deadline; tapping restore returns the server file and clears the badge. After a purge instead, the "only copy" badge appears and export produces a byte-identical file.
- **AC-4** (FR-7) Scripted server events (trash, purge, revoke, new version with auto-update off) against a device with 100 downloaded files: zero local deletions, zero modified bytes without user action.
- **AC-5** (FR-9) Filling the cache cap evicts only cache categories; pinned files and "Removed from server" items survive; the storage manager's numbers match on-disk truth.
- **AC-6** (FR-3, FR-10) In airplane mode, downloaded items are badged and open in the viewer, including a PDF at page anchors whose pages were fetched; non-downloaded items show no misleading state.
