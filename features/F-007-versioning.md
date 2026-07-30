# F-007 — File Versioning & Version Search

**Status:** Draft
**Priority:** P1
**Depends on:** F-001
**Related specs:** [03-storage-and-portability](../specs/03-storage-and-portability.md#versioning-vs-the-folder-is-everything-known-tension), [ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md)

## Summary

Files keep their history. A new upload to an existing path, or a content change detected on disk, creates a new version; the previous version's content is preserved in the app-owned shadow area and its derived data (text, tags-at-the-time, embeddings) remains searchable on request. Search defaults to latest versions only. The current version is always the real file at its real path (portability promise).

## User stories

- As a user, I want to restore an earlier version of a file I overwrote.
- As a user, I want to find a paragraph that was deleted from a document last year by searching *all versions*.
- As a user, I don't want old versions polluting my everyday search results.

## Functional requirements

- **FR-1** New version on: explicit new-version upload, upload to existing path (when so parameterized), or re-scan detecting changed content hash.
- **FR-2** Current version = the real file at the real path; superseded versions' content moves to the app-owned `versions/` area (content-addressed), never visible in the source tree.
- **FR-3** Versions are immutable, listed with timestamp, size, hash, and origin (upload / external change).
- **FR-4** Restore = the chosen version's content becomes a *new* current version (history is never rewritten).
- **FR-5** Derived data (segments, metadata, embeddings) is retained per version → old versions are fully searchable.
- **FR-6** Search defaults to latest; `versions=all` / time-scoped search returns old-version hits explicitly labeled ([F-002/FR-8](F-002-hybrid-search.md)).
- **FR-7** Deleting a file follows the trash-then-purge lifecycle specified in [F-014](F-014-deletion-and-trash.md); purge removes version content (**reference-counted** — content-addressed blobs in `versions/` may be shared across files) and derived data (audited).
- **FR-8** Version retention is policy-driven (count/age/size caps per workspace) — defaults generous, admin-configurable.
- **FR-9** Restorability is explicit ("option b"): app-mediated changes (API upload; later the sync client) preserve the previous content in `versions/` and are restorable. Direct-on-disk edits are detected by re-scan as a new version whose predecessor keeps its derived data (still searchable) but is marked `restorable: false` — its bytes were overwritten before the app could snapshot them.

## API surface

`GET /files/{id}/versions` · `GET /files/{id}/versions/{v}/content` · `POST /files/{id}/versions/{v}/restore` · search parameter `version_scope`.

## Out of scope

Diff views between versions; delta storage (v1 stores full copies — measure before optimizing).

## Open questions

[Q4 (retention defaults & quota accounting)](../OPEN-QUESTIONS.md#q4).

## Acceptance criteria

- Overwriting a file then restoring v1 yields v1's exact bytes as a new v3; all three versions listed.
- A phrase existing only in an old version is found only with `version_scope=all`, labeled as an old-version hit.
- Superseded versions are not visible anywhere in the user's source folder tree.
- After an external in-place edit on the NAS, the file shows a new version; the prior version is findable via `version_scope=all` and reports `restorable: false`.
