# F-014 — Deletion & Trash

**Status:** Draft
**Priority:** P1
**Depends on:** F-001, F-007, F-015
**Related specs:** [02-domain-model](../specs/02-domain-model.md#file) (lifecycle state), [03-storage-and-portability](../specs/03-storage-and-portability.md#deletion--trash), [06-search](../specs/06-search.md#lifecycle-state-scope), [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md#deletion-trash-purge), [10-deployment-and-operations](../specs/10-deployment-and-operations.md#disk-space), [ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md), [ADR-0007](../decisions/ADR-0007-unified-event-log.md)

## Summary

Deletion is a two-step lifecycle: **live → trashed → purged**. Normal deletion always goes to trash — logically instant (the file vanishes from listings and search immediately), physically safeguarded (the current version's content moves into the app-owned, content-addressed `versions/` area, so the source tree only ever contains live files). Trash is restorable for a configurable retention window (default 30 days), then a janitor purges. Purge is the destructive step: it removes every domain row and every stored byte; the only remaining trace is the append-only event log. Files deleted *outside* the app (detected by re-scan) surface in the same trash view, badged accordingly. **The trash promise: the system never removes anything from trash earlier than its deadline** — disk pressure is handled by alerting humans, never by silent early purging.

## User stories

- As a user, I want deleted files to go to a trash I can restore from so that a wrong click is never data loss.
- As a user, I want an immediate "Undo" after deleting so that I can reverse a mistake without hunting through the trash.
- As a user, I want to see what is in my trash, who deleted it, and when it will disappear forever.
- As a user who deleted a whole folder (or suffered a bad bulk action / a misbehaving client), I want to restore everything from that operation in one step.
- As an admin, I want low-disk alerts instead of the system silently destroying trash to free space.

## Functional requirements

- **FR-1** `DELETE` on a file or folder sets state `trashed` — a state change, not a physical operation. Effective immediately: the item disappears from listings, search, facets, counts, autocomplete, and duplicate groups in the same transaction. Trashed items do not reserve their path.
- **FR-2** **Safeguarding:** the trashed file's current-version content is moved into the content-addressed `versions/` area by an internal background job (retry + dead-letter; [03](../specs/03-storage-and-portability.md#deletion--trash)). Until the move completes, the entry cannot be purged and re-scan must not treat the still-present file as new or changed. If the move fails for lack of space on the app volume (cross-filesystem copy), the delete is rolled back with an error that names the cause and offers direct purge as the space-free alternative.
- **FR-3** **Trash listing** per workspace: original path, size, trashed-at, trashed-by, origin (in-app / detected on disk), purge deadline, batch id, restorability, thumbnail (derived data is retained while trashed). Visible to callers with `read` on the entry (evaluated against its original location); cursor-paginated.
- **FR-4** **Restore** returns the file to its original path with identity, versions, tags, metadata, grants, and share links intact (the [F-010/FR-1](F-010-auto-sort-inbox.md) move contract applies to restore); un-trashing creates no new version. If the path is now occupied: reject with `409` unless an explicit alternative path/name is given ([F-001/FR-7](F-001-upload-and-import.md) pattern). Missing extraction facets are re-queued after restore.
- **FR-5** **Batches:** every delete operation gets a batch id (a folder deletion is one batch; so is a bulk action). The whole batch is restorable in one call; individual items inside a batch remain restorable alone. This is the recovery story for mass deletions.
- **FR-6** **Retention:** instance default **30 days**, an admin-editable instance setting stored in PostgreSQL (not env). `0` disables trash (delete purges immediately — documented loudly). Per-workspace overrides in v1: the workspace owner sets a value within admin-configured instance bounds (min/max). A daily janitor purges entries past their deadline; each entry shows its deadline.
- **FR-7** **Purge removes everything:** all domain rows (file, versions, segments, embeddings, derived assets, tags, metadata, grants, share links, trash entry) and all stored content — version blobs reference-counted, since content-addressed blobs in `versions/` may be shared with other files' versions ([F-007/FR-7](F-007-versioning.md)). The event log is the only remaining trace. A purged id is indistinguishable from one that never existed: plain `404` ([08](../specs/08-api-principles.md#errors-rfc-9457)).
- **FR-8** **Purge permissions & friction:** restore and purge require `manage` (the same bar as delete). Per-entry purge, per-workspace "empty trash", and a deliberate direct-purge (trash+purge in one audited call) exist. Admins may empty trash instance-wide in a disk emergency — with typed confirmation, audited; admins see aggregate trash statistics only, never other users' entries ([07](../specs/07-identity-permissions-sharing.md#deletion-trash-purge)).
- **FR-9** **No automatic early purge, ever.** Disk pressure triggers admin alerts and blocks space-needing writes with a clear error ([10](../specs/10-deployment-and-operations.md#disk-space)); purge and empty-trash operations are never blocked by a full disk.
- **FR-10** **Out-of-band deletions:** a file that re-scan finds missing on disk becomes a trash entry badged "removed outside the app", subject to the same retention clock. It is restorable only to its latest app-held version; with none, it is shown `restorable: false` ([F-007/FR-9](F-007-versioning.md) pattern). If content with the same hash reappears at the same path (re-scan or re-upload), the original file row is reactivated — identity, tags, and history return.
- **FR-11** **Shares & permissions while trashed:** grants are preserved but inert; share links are suspended, not revoked — they stop serving on trash and resume on restore; purge revokes them permanently. Whether a suspended link answers `404` or `410` is open (Q21).
- **FR-12** **Search:** trashed items are excluded from every default query surface by construction — enforced *inside* the query, including inside vector (ANN) search, never post-hoc ([06](../specs/06-search.md#lifecycle-state-scope)). An explicit state scope `live` (default) / `trashed` / `all` exists ([F-002/FR-13](F-002-hybrid-search.md)); `trashed` hits require the same permission as the trash listing and are labeled.
- **FR-13** **Workspace deletion** requires the workspace's exact name as explicit confirmation (API field, typed in the UI), is restricted to the owner or an admin, and produces one restorable trash batch; the workspace row is tombstoned until purge.
- **FR-14** **Pipeline interaction:** trashing cancels queued extraction jobs and aborts running ones for the item; reprocessing ([F-009](F-009-reprocessing.md)) skips trashed items — after restore, a stale-generation file appears as a normal reprocessing candidate.

## API surface

`DELETE /files/{id}` · `POST /files/{id}/restore` · `POST /files/{id}/purge` · `DELETE /folders/{id}` ([F-015](F-015-folders.md)) · `DELETE /workspaces/{ws}` (requires `confirm: "<name>"`) · `GET /workspaces/{ws}/trash` · `POST /workspaces/{ws}/trash/empty` · `POST /trash/restore` (batch id or item ids) · retention settings via instance/workspace settings endpoints · events `file.trashed|restored|purged`, `folder.…`, batch variants.

## Out of scope

Version-level retention/pruning (that is [Q4](../OPEN-QUESTIONS.md)). Upstream deletions in external workspaces (Q16 — they will map onto the FR-10 state). Notifying affected users when an admin empties trash (needs the future notification feature). Automatic early purge under any circumstances.

## Open questions

[Q21 (trashed share links: 404 vs 410)](../OPEN-QUESTIONS.md). Retention bounds defaults (min/max for workspace overrides) — ops tuning at release time.

## Acceptance criteria

- Deleting a file removes it from listings and search results within the same request; restore within the window returns it with byte-identical content, same UUID, and unchanged tags, versions, grants, and share links.
- After purge, `GET /files/{id}` is indistinguishable from a random UUID (`404`, identical body shape); no domain table contains a row for it; its events remain queryable.
- Two files with identical content: purging one leaves the other's version content fully restorable (blob refcount).
- A trashed file never appears in search results, facets, counts, autocomplete, or duplicate groups — including for semantic-only queries (leak test, same rigor as permission tests); `state: trashed` returns it for an authorized caller, labeled.
- Deleting a folder yields one batch; batch restore recreates the subtree including empty subfolders; restoring a single file out of the batch works.
- Restore into an occupied path returns `409` without an explicit target; succeeds with one.
- Re-uploading identical content at a trashed file's old path reactivates the same file UUID with its history.
- An entry past its deadline is purged by the janitor; a per-workspace override outside admin bounds is rejected.
- Workspace deletion without the exact `confirm` name fails; with it, the workspace disappears and is restorable as one batch.
- A share link serves `4xx` while its target is trashed, works again after restore, and is gone after purge.
- With the app volume full: purge and empty-trash succeed; uploads fail with the documented out-of-space problem type; nothing in trash is removed before its deadline.
- Deleting a file directly on the NAS produces (after re-scan) a "removed outside the app" trash entry; with no app-held version it reports `restorable: false`.
