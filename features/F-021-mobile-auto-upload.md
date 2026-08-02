# F-021 — Mobile Auto-Upload (One-Way Device Backup)

**Status:** Draft
**Priority:** P1
**Clients:** Android, iOS — native-only because browsers cannot enumerate device media, read folders unattended, or run background work; the API surface this feature adds (`hash-check`) is client-agnostic and serves future sync clients too
**Depends on:** [F-001](F-001-upload-and-import.md) (upload sessions), [F-007](F-007-versioning.md) (modified items become versions), [F-019](F-019-mobile-connection.md)
**Related specs:** [13-mobile-clients](../specs/13-mobile-clients.md) (sources, ledger, one-way principle — normative), [03-storage-and-portability](../specs/03-storage-and-portability.md#uploads), [12-reliability](../specs/12-reliability.md#client-visible-idempotency), [02-domain-model](../specs/02-domain-model.md#metadataentry) (asset-group keys)

## Summary

Continuous one-way backup from the phone into chosen cloud destinations. The app discovers device sources (Android: media folders across all volumes and SAF folder grants; iOS: photo library, albums, and Files-app folders), suggests them, and per source uploads **byte-identical originals** — existing content first (backfill), new content as it appears, edits as new versions. Every item advances through the durable ledger of [13-mobile-clients](../specs/13-mobile-clients.md); *verified* means the server confirmed the bytes by content hash, which is what [F-022](F-022-device-storage-reclaim.md) later relies on. The queue is exhaustively observable: an in-scope item is always in exactly one accounted state — a backup product that can silently miss files is broken by definition.

## User stories

- As a user, I want the app to find every folder containing photos, videos, or music and suggest backing them up so that I don't hunt for paths.
- As a user, I want each source to go to a folder I choose on the server so that my cloud stays organized my way.
- As a user, I want photos I already had *and* new ones backed up automatically so that "enabled" means complete, not "from now on".
- As a user, I want to see exactly what is uploaded, pending, or failed — and why — so that I can trust deletion later.

## Functional requirements

### Sources & configuration

- **FR-1** Source types per [13 § sources](../specs/13-mobile-clients.md#sources): *(Android)* media folders (MediaStore buckets, all volumes incl. SD) and SAF folder grants; *(iOS)* photo library ("All photos"), albums incl. smart albums with include/exclude, and Files-app folders via persistent bookmarks. Files-app/SAF-grant sources carry the UI label "scans when you open the app" wherever their state is shown *(iOS Files folders have no background change detection — labeled honesty, not fine print)*.
- **FR-2** *(Android)* Discovery enumerates every media-containing folder across volumes with per-folder counts by media class and sample thumbnails, and presents a suggestion list (camera and screenshot folders preselected-suggested, messenger folders labeled as such).
- **FR-3** *(iOS)* Discovery lists the photo library with total counts and all albums/smart albums with counts; "All photos" is the default offer — albums are labels, not partitions, and a photo in no album must not be missable.
- **FR-4** *(negative space)* Reduced media access (Android 14+ partial grant, iOS limited library) is detected and surfaced as a persistent warning state with a fix action; the app never reports a source as fully backed up while its visibility is partial.
- **FR-5** Per-source configuration, inspectable and editable at any time: enabled/paused · **destination = workspace + folder** (default suggestion `Devices/{device-name}/{source-name}/`, freely changeable) · optional `YYYY/MM` subfoldering by capture date (else file date) · media-class filter where applicable · network policy (Wi-Fi-only default, cellular opt-in) · charge-only option.
- **FR-6** Enabling a source enqueues **all currently matching items**, not only future ones; the backfill is resumable and survives app restarts (ledger, [13](../specs/13-mobile-clients.md#the-upload-ledger)).
- **FR-7** An item matched by multiple enabled sources uploads exactly once; its destination is the highest-priority matching source in the user-ordered source list (deterministic and displayed).

### Upload pipeline

- **FR-8** Uploaded bytes are **identical to the source item**: the stored version's content hash equals the local hash. *(Android)* reading uses `ACCESS_MEDIA_LOCATION` + `setRequireOriginal`; if that permission is missing, affected items enter `blocked: needs permission` rather than uploading location-stripped bytes. *(iOS)* originals come from asset resources, never rendered exports.
- **FR-9** *(negative space)* No upload path ever transcodes, re-encodes, resizes, or strips metadata. Motion Photos upload as their single original file, untouched.
- **FR-10** *(iOS)* iCloud-optimized items (original not on device) are fetched from iCloud before upload, with per-item progress and the source's network policy applied; fetch failures are surfaced per item with cause.
- **FR-11** Paired captures upload **all components** and stamp the asset-group keys (`asset_group`, `group_role`, `group_kind` — [02](../specs/02-domain-model.md#metadataentry), [13 § paired captures](../specs/13-mobile-clients.md#paired-captures-asset-groups)): Live Photos as photo + paired video (`motion-pair`), RAW+JPEG as both files (`raw-jpeg`). The ledger marks the item verified only when every component is verified.
- **FR-12** Before uploading, item hashes are checked in batch against the server (`POST /files/hash-check`); a hash that exists as a **live file version in a workspace the user owns** marks the item verified without transfer, recording the existing file as its mapping. A trashed match does not count and the upload proceeds.
- **FR-13** Transfers use the resumable upload sessions of [F-001/FR-2](F-001-upload-and-import.md) with `Idempotency-Key` ([12](../specs/12-reliability.md#client-visible-idempotency)); interrupted transfers resume from the acknowledged offset, and no retry can create a duplicate file or duplicate version.
- **FR-14** A source item whose bytes change (same device identity, new hash) uploads as a **new version of its mapped file** ([F-007/FR-1](F-007-versioning.md)); its verification state resets until the new version is confirmed. *(iOS)* an edited photo maps to: original resource as the first version, the current adjusted rendition as a subsequent version; reverting an edit re-produces bytes the server already holds and transfers nothing.
- **FR-15** *(negative space)* Auto-upload never writes to, renames, moves, or deletes any device-side file. Device-side renames, moves, and deletions have no server effect ([13 § one-way](../specs/13-mobile-clients.md#the-one-way-principle)).
- **FR-16** *(negative space)* No server-side event — deletion, move, permission change, new version from another client — causes any modification or deletion of device-side files by this feature.

### Scheduling, health & observability

- **FR-17** New-item pickup, platform-honest ([13 § background matrix](../specs/13-mobile-clients.md#platform-background-execution-matrix)): *(Android)* content-change triggers plus a periodic catch-up scan; a new item is queued within 15 minutes under default conditions (device on Wi-Fi, not battery-restricted) even if the trigger was missed. *(iOS ≥ 26.1)* the system background-upload extension is registered, so photo uploads proceed with the app closed. *(iOS, all versions)* opening the app reconciles completely via the photo-library change history; Files-folder sources rescan on open. The UI always shows each source's last-completed-scan time.
- **FR-18** A **backup health** screen reports every precondition with a fix action: media-access completeness (FR-4), location-metadata permission *(Android)*, battery-optimization exemption *(Android)*, Background App Refresh / upload-extension state *(iOS)*, iCloud Photos status *(iOS)* — plus a manual **"scan now"** that performs a full reconcile of all sources.
- **FR-19** *(negative space)* **Exhaustive accounting:** every item in an enabled source appears in exactly one ledger state (`discovered / hashed / queued / uploading / uploaded / verified / skipped-duplicate / blocked / failed`) with per-state counts; enumerating a source and diffing against the ledger yields zero unaccounted items. Failed and blocked items are listed individually with cause and a retry action.
- **FR-20** Overall status is presented as items remaining ("N of M backed up · X GB to go") plus per-source status; local notifications are emitted **only** for actionable conditions (authentication expired, destination missing/permission lost, storage full, persistent failures) — never for routine progress beyond the OS-required transfer notification *(Android foreground service)*.
- **FR-21** *(verify: benchmark)* Energy: backing up 1 GB of media over Wi-Fi consumes ≤ 2 % battery on the [Q27](../OPEN-QUESTIONS.md) reference devices.
- **FR-22** After app reinstall (empty ledger), re-enabling the same sources converges via FR-12 to all previously uploaded items `verified` with **zero bytes of media re-transferred**.

## API surface

Consumes: `POST /workspaces/{ws}/files` (chunked/resumable, [F-001](F-001-upload-and-import.md)) · `PUT /files/{id}/content` (new version) · metadata write for asset-group keys. **Adds:** `POST /files/hash-check` — batch content-hash lookup: request `[{hash, size}]` (bounded batch), response per hash `{exists, file_id?, workspace?, state}` scoped to live versions in workspaces the caller owns; permission rules identical to search ([F-002/FR-7](F-002-hybrid-search.md)) — the endpoint reveals nothing about other users' content.

## Out of scope

Two-way sync / mirroring (explicit non-goal — [13](../specs/13-mobile-clients.md#the-one-way-principle)). Deleting device files ([F-022](F-022-device-storage-reclaim.md)). Desktop sync clients (deferred, same primitives). iOS music (no file access exists). Geofence/iBeacon wake boosters ([Q47](../OPEN-QUESTIONS.md)). Capture-time hints for EXIF-less files ([Q44](../OPEN-QUESTIONS.md)).

## Open questions

[Q38 (upload wire protocol — iOS extension compatibility)](../OPEN-QUESTIONS.md) · [Q44](../OPEN-QUESTIONS.md) · [Q45 (store distribution & permission review)](../OPEN-QUESTIONS.md) · [Q47](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-2, FR-5, FR-6) Enabling the suggested Camera folder with destination `Photos/Pixel/Camera/` uploads every existing photo and a subsequently taken one; each stored version's hash equals the device file's hash; paths follow the mapping (and `2026/08/…` with subfoldering on).
- **AC-2** (FR-8) *(Android)* A GPS-tagged photo round-trips with EXIF location intact; with `ACCESS_MEDIA_LOCATION` revoked, the item shows `blocked: needs permission` and no upload occurs.
- **AC-3** (FR-11) A Live Photo yields two files stamped with one `asset_group` id and roles `primary`/`motion-video`; the library shows one tile ([13 § UI contract](../specs/13-mobile-clients.md#paired-captures-asset-groups)); the item reports verified only after both components verify.
- **AC-4** (FR-14) Editing an uploaded photo produces version 2 of the same file id; version 1 remains restorable ([F-007](F-007-versioning.md)); the item is not reclaim-eligible until v2 verifies.
- **AC-5** (FR-15, FR-16) Deleting a backed-up photo on the phone changes nothing server-side; trashing its file server-side changes nothing on the phone — both asserted, not assumed.
- **AC-6** (FR-12, FR-22) After reinstall and source re-enable against a library of 10 000 already-uploaded photos, all items reach `verified` and the network trace contains hash-check batches but zero media uploads.
- **AC-7** (FR-19) With one item made unreadable mid-queue, source enumeration minus ledger accounting is empty and the item appears under `failed` with its cause; nothing is silently absent.
- **AC-8** (FR-4) Granting partial photo access surfaces the warning state and backup status shows "partial visibility", never "complete".
- **AC-9** (FR-17) *(Android)* With the app backgrounded under default conditions, a new photo is queued within 15 minutes; the health screen's last-scan stamp updates.
- **AC-10** (FR-13) Killing the app mid-4 GB-video upload and relaunching resumes from the recorded offset and produces exactly one file version ([F-001](F-001-upload-and-import.md) AC parity).
