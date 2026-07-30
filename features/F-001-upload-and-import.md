# F-001 — File Upload & Workspace Import

**Status:** Draft
**Priority:** P0
**Depends on:** —
**Related specs:** [03-storage-and-portability](../specs/03-storage-and-portability.md), [04-ingestion-pipeline](../specs/04-ingestion-pipeline.md)

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
- **FR-5** Import at 10 TB scale is resumable and incremental: restarts continue where they left off; progress (files found / registered / extracted) is queryable via API.
- **FR-6** Re-scan reconciles external changes: new file → register + ingest; changed content (hash differs) → new version ([F-007](F-007-versioning.md)); missing file → marked missing/deleted, never silently purged from the index.
- **FR-7** Name collisions on upload are handled predictably (reject or new-version, per explicit parameter; default: reject with clear error).
- **FR-8** Every registered file is immediately visible in listings with basic file metadata, with extraction status `pending` — searchability by content follows as extractors complete.

## API surface

`POST /workspaces/{ws}/files` (chunked upload) · `POST /workspaces` (with `import_path`) · `POST /workspaces/{ws}/rescan` · `GET /workspaces/{ws}/import-status` · `GET /files/{id}` (incl. extraction status)

## Out of scope

Sync clients, WebDAV, mobile upload (deferred; see [08-api-principles](../specs/08-api-principles.md) for the primitives that keep them possible). Auto-sorting ([F-010](F-010-auto-sort-inbox.md)).

## Open questions

[Q2 (mount↔workspace mapping)](../OPEN-QUESTIONS.md#q2), [Q3 (change detection: watch vs. scheduled re-scan)](../OPEN-QUESTIONS.md#q3).

## Acceptance criteria

- Upload of a 4 GB file over an unreliable connection completes via resume; hash of stored file equals hash of source.
- Importing a subtree with 100k files registers all of them; killing and restarting the importer mid-run yields the same final state.
- A file copied onto the NAS by hand appears in the app after re-scan with correct path and queued extraction.
- No import or upload path ever modifies an original file's bytes or location.
