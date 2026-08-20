# F-001 — File Upload & Workspace Import

**Status:** Draft
**Priority:** P0
**Clients:** all
**Depends on:** —
**Related specs:** [03-storage-and-portability](../specs/03-storage-and-portability.md), [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md), [12-reliability](../specs/12-reliability.md), [ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md), [ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md), [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)

## Summary

Get files into the system two ways: upload through the API into a chosen workspace path, and import an existing folder hierarchy by pointing a workspace at a subtree of the mounted storage. Both converge on the same result: real files at real paths, registered, hashed, and queued for extraction. Nothing blocks on analysis.

## User stories

- As a user, I want to upload files (including multi-GB videos) into a folder of my workspace so that they are stored and become searchable.
- As a new user, I want to import my existing NAS folder structure as-is so that I can adopt the app without reorganizing anything.
- As a user, I want files I copy onto the NAS directly (outside the app) to show up in the app so that both paths stay usable.

## Functional requirements

- **FR-1** Upload targets a workspace + path; the file is written to that real path on the source storage. Original bytes are stored unmodified.
- **FR-2** Uploads are chunked/resumable; an interrupted multi-GB upload resumes rather than restarts.
- **FR-3** Upload responds as soon as the file is safely stored; extraction is queued asynchronously (`202`-style, job reference returned).
- **FR-4** Import: a workspace can be created over an existing subtree; the scanner registers every file (path, size, mtime, content hash) without moving, renaming, or modifying anything.
- **FR-5** *(verify: fault-injection)* Import at 10 TB scale is resumable and incremental: restarts continue where they left off; progress (files found / registered / extracted) is queryable via API.
- **FR-6** Re-scan reconciles external changes: new file → register + ingest; changed content (hash differs) → new version ([F-007](F-007-versioning.md)); missing file → a trash entry badged "removed outside the app" ([F-014/FR-10](F-014-deletion-and-trash.md)), never silently purged from the index.
- **FR-7** Name collisions on upload are handled predictably (reject or new-version, per explicit parameter; default: reject with clear error). A collision is a clash on the comparison key ([F-015/FR-6](F-015-folders.md)), so uploading `Report.pdf` beside an existing `report.pdf` collides.
- **FR-8** Every registered file is immediately visible in listings with basic file metadata, with extraction status `pending` — searchability by content follows as extractors complete.
- **FR-9** **Wire protocol** ([ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md)): the upload endpoint implements the IETF resumable-upload protocol — `OPTIONS` answers `200` with `Upload-Limit`; a creation request carrying `Upload-Complete: ?0` answers with an interim `104` and a `Location`; `HEAD` on that resource reports the current `Upload-Offset`; a `PATCH` append at a wrong offset answers `409` carrying the correct one; `DELETE` cancels. A request whose `Upload-Draft-Interop-Version` is absent or unsupported is served as an ordinary upload and receives no `104`.
- **FR-10** **Adoption is admin-gated** ([ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)): creating a workspace over an existing directory requires an admin and a path resolving inside the `SE_ADOPTION_ROOTS` allow-list; a path outside it, a path overlapping another workspace root, or a filesystem failing the `fs-check` probe is refused with a problem response naming the reason. Non-admin callers cannot adopt at all.
- **FR-11** **Scan conflicts are reported, never resolved on disk** ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)): when a scan finds sibling names colliding on the comparison key (NFC-normalized, case-folded), the first in traversal order registers and each other is listed as a conflict with both names; no file on disk is renamed, moved, deleted, or overwritten by the app to resolve one.
- **FR-12** *(negative space)* **No path outside the workspace root is ever read or written**: symlinks are not dereferenced by any scan (each is recorded as a skipped entry), and every content read or write re-resolves its path and refuses one that leaves the workspace root — including a dangling link and a link whose lexical path looks contained.
- **FR-13** *(negative space)* The `.workspace/` control directory and everything inside it is **never** registered as a folder or file, returned in a listing, or served as content.

## API surface

`POST·OPTIONS /workspaces/{ws}/files` (upload creation, `Upload-Limit`) · `HEAD·PATCH·DELETE /uploads/{id}` (offset, append, cancel) · `POST /workspaces` (with `adopt_path`, admin) · `POST /workspaces/{ws}/rescan` (whole workspace or a subtree) · `GET /workspaces/{ws}/import-status` (incl. scan conflicts and skipped entries) · `GET /files/{id}` (incl. extraction status)

## Out of scope

Desktop sync clients, WebDAV (deferred; see [08-api-principles](../specs/08-api-principles.md) for the primitives that keep them possible). Mobile auto-upload is now specified in [F-021](F-021-mobile-auto-upload.md) and consumes this feature's upload sessions unchanged. Auto-sorting ([F-010](F-010-auto-sort-inbox.md)).

## Open questions

None open. The decisions this feature waited on are recorded in [ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md) (wire protocol, Q38), [ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md) (workspace layout and adoption, Q2 + Q31), and [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md) (names, symlinks, change detection, filesystem requirements — Q25, Q22, Q3, Q32). Per-workspace quotas remain out of scope here (Q57).

## Acceptance criteria

- **AC-1** (FR-2, FR-9) A 4 GB upload is interrupted mid-body and resumed: `HEAD` reports the committed offset, the client appends from there, and the stored file's SHA-256 equals the source's. A `PATCH` sent at a stale offset answers `409` with the current offset, and the file is not corrupted by it.
- **AC-2** (FR-9) `OPTIONS` on the upload endpoint answers `200` with `Upload-Limit`; a creation request declaring interop version 9 receives a `104` with a `Location`; the same request with no interop-version header receives no `104` and still stores the file.
- **AC-3** (FR-4, FR-5) Importing a subtree with 100 000 files registers all of them; killing the importer at each injected fault point and restarting yields the same final state, with no duplicate registrations and no debris past the grace window ([12 § verification](../specs/12-reliability.md#verification)).
- **AC-4** (FR-6) A file copied onto the storage by hand appears after the scheduled scan with correct path and queued extraction; an externally modified file yields a new version; an externally deleted file becomes a trash entry badged "removed outside the app" and is never silently dropped from the index.
- **AC-5** (FR-10) A member's attempt to adopt any path is refused; an admin adopting a path outside `SE_ADOPTION_ROOTS`, or overlapping an existing workspace root, is refused with the reason named; an admin adopting an allow-listed directory indexes it with **zero** bytes copied and zero entries renamed.
- **AC-6** (FR-11) A tree containing `Report.pdf` and `report.pdf` as siblings, plus an NFC/NFD pair of one name, imports one of each and lists the others as conflicts naming both spellings; a byte-for-byte comparison of the tree before and after the scan shows it unchanged.
- **AC-7** (FR-12) A symlink to `/etc/passwd` and a symlink to a directory outside the workspace are both recorded as skipped, appear in no listing, and their targets are never read; requesting content through a path that resolves outside the root is refused.
- **AC-8** (FR-13) After an upload and a scan, no listing, search, or content endpoint returns `.workspace`, its `marker`, or any staging entry.
- **AC-9** (FR-1, FR-4) No import or upload path ever modifies an original file's bytes or location — asserted by hashing every fixture before and after a full import plus an upload into the same tree.
