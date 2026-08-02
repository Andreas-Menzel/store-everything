# F-023 — OS File-Manager Integration

**Status:** Draft
**Priority:** P2
**Clients:** Android, iOS — native-only because it is built on OS extension points (Android `DocumentsProvider`, iOS File Provider); the web equivalent is the web app itself
**Depends on:** [F-019](F-019-mobile-connection.md), [F-001](F-001-upload-and-import.md), [F-007](F-007-versioning.md), [F-014](F-014-deletion-and-trash.md), [F-015](F-015-folders.md)
**Related specs:** [13-mobile-clients](../specs/13-mobile-clients.md), [08-api-principles](../specs/08-api-principles.md)

## Summary

The cloud appears inside the operating system's own file surfaces — the iOS Files app and Android's document picker/file managers — the way Nextcloud does it: browse every readable workspace **without downloading anything** (metadata-only placeholders with cloud badges), download on demand with visible progress and state, open files in other apps in place, and write back. Writes are ordinary API operations: a save creates a new version ([F-007](F-007-versioning.md)), a delete goes to the server trash ([F-014](F-014-deletion-and-trash.md)) — the OS surface gets no privileged path and no second semantics.

## User stories

- As a user, I want my cloud to show up in the Files app / file picker so that any app can open my cloud documents.
- As a user, I want to browse huge folders without downloading them so that browsing is free and downloads are deliberate.
- As a user, I want edits saved from another app to become new versions so that the OS surface is as safe as the API.

## Functional requirements

- **FR-1** The provider exposes one root per connected account containing every workspace and folder the account can read, browsable with metadata only — listing a folder transfers no file content.
- **FR-2** Opening an un-downloaded file fetches it on demand with OS-visible progress; completed content is hash-verified ([13 § integrity](../specs/13-mobile-clients.md#caching-downloads-integrity)) before being handed to the requesting app. *(iOS)* opening downloads the whole file — the platform offers no ranged materialization; stated, not worked around.
- **FR-3** Per-file download state (cloud-only / downloading / downloaded) is reported through the platform's own indicators; *(iOS)* keep-downloaded and evict use the Files app's native actions.
- **FR-4** The provider serves thumbnails for image/video/document entries from the existing thumbnail endpoint, cached under the [F-020/FR-6](F-020-mobile-library.md) cache rules.
- **FR-5** Writes map 1:1 to the API: save-back = new version of the same file · create/copy-in = normal upload into the target folder ([F-001](F-001-upload-and-import.md)) · rename/move = the move operation ([F-015/FR-4](F-015-folders.md)) · delete = server trash ([F-014/FR-1](F-014-deletion-and-trash.md)), never a hard delete.
- **FR-6** *(negative space)* The provider exposes nothing beyond the account's `read` scope — a permission revocation removes the affected subtree from the OS surface on next enumeration, and no cached listing can serve entries the account can no longer read.
- **FR-7** *(negative space)* Provider operations neither trigger auto-upload logic nor count toward reclaim eligibility ([F-021](F-021-mobile-auto-upload.md), [F-022](F-022-device-storage-reclaim.md)) — the OS surface manages *cloud* content; the backup ledger manages *device* content. OS-initiated cache eviction of a provider file never touches server state.
- **FR-8** Offline behavior: downloaded provider files open without connectivity; operations requiring the server fail with the platform's offline signaling, never by silently dropping the operation ([F-019/FR-6](F-019-mobile-connection.md) rule applied to the provider).

## API surface

Adds no endpoints — consumes listing, content (Range), thumbnail, upload, move, and trash surfaces exactly as the in-app features do.

## Out of scope

Exposing the auto-upload ledger or device sources through the provider. Provider-level sharing UI (share links are in-app — [F-025](F-025-client-parity.md)). Third-party apps granting *their* folders to us (that is a source concern — [F-021](F-021-mobile-auto-upload.md)).

## Open questions

None feature-local. Provider visibility quirks of specific third-party file managers are documented behavior, not spec.

## Acceptance criteria

- **AC-1** (FR-1, FR-2) Browsing a 10 000-entry folder in the OS picker transfers only listings (trace: no content bytes); opening one PDF downloads it with progress, verifies its hash, and opens it in a third-party viewer.
- **AC-2** (FR-5) Editing that PDF in the third-party app and saving produces version 2 of the same file id server-side; version 1 is restorable.
- **AC-3** (FR-5) Deleting a file in the OS surface produces a server trash entry restorable from the web app; no purge occurs.
- **AC-4** (FR-6) Revoking the account's read grant on a folder: the next OS-surface enumeration no longer contains it, and a direct open of a previously listed entry fails permission-clean.
- **AC-5** (FR-7) A file downloaded via the provider then evicted by the OS leaves the server file untouched and never appears in reclaim's eligible set.
