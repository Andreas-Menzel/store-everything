# F-015 — Folders

**Status:** Draft
**Priority:** P0
**Clients:** all
**Depends on:** F-001
**Related specs:** [02-domain-model](../specs/02-domain-model.md#folder), [03-storage-and-portability](../specs/03-storage-and-portability.md), [06-search](../specs/06-search.md), [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md#ownership-and-permissions), [ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md), [ADR-0006](../decisions/ADR-0006-hierarchical-tags-dag.md) (closure-table pattern)

## Summary

Folders are first-class domain objects, not path strings. Each folder has a **UUID that survives rename and move** (the same identity rule files already have), which is what permission grants, tags, and future folder share links attach to — a subtree grant must not evaporate because the folder was renamed. The on-disk directory tree remains the source of truth (ADR-0003): folder entities mirror it 1:1, created and reconciled by upload, import, and re-scan. Folders carry system-computed aggregates (file count, total size) and can be tagged and found in search.

## User stories

- As a user, I want to grant someone access to a folder and have that grant survive renaming or moving the folder.
- As a user, I want to see how many files and how much data a folder contains without counting by hand.
- As a user, I want to create, rename, and move folders — including across workspaces — with tags and permissions traveling along.
- As a user who renames a directory directly on the NAS, I want the app to recognize it as the same folder so that grants and tags survive.
- As a user, I want to tag a folder (e.g. `tax`) and find it again by that tag.

## Functional requirements

- **FR-1** **Entity & identity:** a folder is `(uuid, workspace, parent, name)`; every workspace has an auto-created root folder (no parent; cannot be renamed, moved, or trashed — those are workspace operations). Sibling names are unique within a parent on the comparison key of [FR-6](#functional-requirements). The UUID survives rename and move; grants, tags, and (future) share links attach to it.
- **FR-2** **Ancestry via closure table** (the ADR-0006 pattern reused): one precomputed ancestor relation powers subtree permission checks (one indexed join in the permission filter — the hottest query in the product), path-prefix search filters, and cycle detection. Permission evaluation is live: moving an item into or out of a granted subtree changes effective permissions immediately.
- **FR-3** **Files hang off folders:** a file is `(folder, name)`; its display path is derived from the folder chain, no longer stored as an independent string ([02](../specs/02-domain-model.md#file)).
- **FR-4** **Operations** — create, rename, move, trash ([F-014](F-014-deletion-and-trash.md)) — are first-class API operations mirrored to disk. Move rules: into one's own descendant → rejected (cycle); name collision at the destination → rejected, no merge in v1 ([F-001/FR-7](F-001-upload-and-import.md) pattern); the workspace root is not movable ([FR-1](#functional-requirements)); **cross-workspace moves are supported while both workspace roots are on one filesystem**, where the move is a single atomic rename — contained files keep identity, versions, tags, grants, and share links ([F-010/FR-1](F-010-auto-sort-inbox.md)); folder-attached grants and tags travel; grants scoped to the *source workspace* naturally stop covering the moved subtree. A move **between filesystems** is refused with that reason rather than degraded into a byte-for-byte copy inside a request: copying a subtree that may be terabytes is a long-running resumable operation, and it is deferred to [FR-14](#functional-requirements).
- **FR-14** **Cross-filesystem folder move** *(deferred — see [ROADMAP](../ROADMAP.md))*: moving a folder between workspaces whose roots are on different filesystems copies its content as a resumable, checkpointed operation, verifies each file's hash before removing the source, and leaves the source intact if it is interrupted. Until it exists, FR-4's refusal is the whole behaviour. *(verify: fault-injection)*
- **FR-5** **Listing:** `GET /folders/{id}/children` returns subfolders and files, cursor-paginated (directories with 100k entries must stay usable), sortable by name, size, and modification time. **Subfolders come first, ordered by name; the sort applies to the files** — ordering the two together by size would have to place directories by a recursive aggregate that is still converging ([FR-8](#functional-requirements)), so a page's order could shift under a client mid-pagination. One cursor spans both segments, and a page is filled across the seam rather than cut short at it; the cursor carries the ordering it was made under, and reusing it under another is refused.
- **FR-6** **Naming rules** (shared with files, normative in [03 § names on disk](../specs/03-storage-and-portability.md#names-on-disk), decided in [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)): names are stored as given; siblings must be unique on the **comparison key** = NFC-normalized + case-folded name, so `Foo`/`foo` and the NFC/NFD spellings of one name are rejected as collisions; API-supplied names are normalized to NFC; a rename changing only case or only normalization is a rename, not a no-op; `.workspace` is reserved at the workspace root; `/` and control characters are rejected; limits are 255 bytes per name and 4096 bytes per workspace-relative path.
- **FR-7** **Re-scan identity:** a directory renamed or moved on disk looks like delete+create. After file-level reconciliation, if the majority of a vanished folder's known files re-matched under a single new directory, the folder's UUID transfers (grants and tags survive external renames). Ambiguous cases (splits, merges) create new identities and emit an audit event; a review UX is a possible later addition (Q24). An *empty* folder renamed on disk cannot be matched and gets a new identity — documented limitation.
- **FR-8** **Aggregates:** each folder exposes direct file count, recursive file count, and recursive size — maintained asynchronously from the event stream with coalesced ancestor-chain rollups. Eventually consistent with an `as_of` stamp; target: a change is reflected within ~5 s (p95). Folder moves update aggregates in O(depth) (add/subtract the moved subtree's own totals). A low-priority janitor periodically recomputes a rotating subset from ground truth and flags drift. (Synchronous rollups are rejected: every upload would contend on the workspace-root row and throttle import.)
- **FR-9** **Tags on folders — manual, self-only (v1):** users with `write` tag folders from the same global vocabulary; extractors never run on folders, so folder tags have no confidence/generation machinery. A folder tag describes the folder itself and does **not** match contained files in tag searches; query-time inheritance to contents is deliberately deferred (Q23).
- **FR-10** **Folders are searchable objects:** matched by name, tags, and metadata (they have no content segments), returned as folder-typed results ([F-002/FR-14](F-002-hybrid-search.md)), permission-filtered like everything else. Manual metadata entries on folders are supported.
- **FR-11** **Events:** `folder.created/renamed/moved/trashed/restored/purged` in the unified event log (audit, live updates, change feed).
- **FR-12** **Caller-relative paths (visibility roots):** every path the API returns is derived from the caller's **visibility root** — the topmost ancestor folder the caller can read ([07 § visibility roots](../specs/07-identity-permissions-sharing.md#visibility-roots-what-a-grantee-sees)). Ancestor references above that root are omitted from responses (a grant-root folder's `parent`, the parent folder of a file granted individually); requesting an unreadable ancestor returns `404`. For owners the visibility root is the workspace root.
- **FR-13** *(negative space)* No response to a caller contains the name, id, path segment, aggregate count, or event of any folder above that caller's visibility root — verified with [F-002/FR-7](F-002-hybrid-search.md) leak-test rigor.

## API surface

`POST /workspaces/{ws}/folders` (parent + name) · `GET /folders/{id}` (metadata + aggregates, `as_of`) · `GET /folders/{id}/children` (`sort=name|size|modified`, cursor; subfolders first) · `POST /folders/{id}/move` (rename/move, incl. cross-workspace) · `POST /files/{id}/move` (the same operation for one file — [F-010/FR-1](F-010-auto-sort-inbox.md)) · `GET·POST·DELETE /folders/{id}/tags` · `DELETE /folders/{id}` ([F-014](F-014-deletion-and-trash.md)) · folder scope in `POST /permissions` now references folder UUIDs.

## Out of scope

Folder share links (deferred in [F-008](F-008-sharing-and-public-links.md); this entity is the prerequisite). Folder thumbnails (type icon in v1). Merge-on-move. Tag inheritance to contents (Q23). Per-user folder favorites.

## Open questions

[Q23 (folder-tag inheritance)](../OPEN-QUESTIONS.md) · [Q24 (identity review queue)](../OPEN-QUESTIONS.md). Names (Q25) and symlinks (Q22) are decided in [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md).

## Acceptance criteria

- **AC-1** (FR-1, FR-4) Renaming a folder via API keeps its UUID; grants and tags are intact; contained files report updated paths; the directory is renamed on disk.
- **AC-2** (FR-7) Renaming a directory with content directly on disk: after re-scan the folder keeps its UUID and grants. Renaming an *empty* directory yields a new identity (documented behavior, asserted).
- **AC-3** (FR-4) Moving a folder into its own descendant fails with a problem response naming the cycle; moving onto an existing sibling name fails without an explicit resolution.
- **AC-4** (FR-4) A cross-workspace folder move preserves all contained file UUIDs, tags, grants, and share links; a grant scoped to the source workspace no longer applies to the moved subtree; a grant scoped to the moved folder still does.
- **AC-5** (FR-2, FR-10) A grantee with `read` on folder F finds files under F in search; after a file is moved out of F, the grantee can no longer access or find it.
- **AC-6** (FR-6) Creating `Foo` beside an existing `foo`, or the NFD spelling beside the NFC one, is refused as a collision; renaming `foo` to `Foo` succeeds and is reflected on disk; a name of 256 bytes is refused.
- **AC-7** (FR-8) After uploading 1 000 files into a deep folder, ancestor counts/sizes converge within the documented window and match ground truth exactly; the janitor detects an artificially corrupted aggregate.
- **AC-8** (FR-9) A folder tagged `tax` is returned as a folder-typed search hit for `tag:tax`; a file inside it is *not* matched by that tag (v1 semantics).
- **AC-9** (FR-5) Listing a folder with 100k children paginates stably by cursor.
- **AC-10** (FR-12) Alice grants Bob `read` on `/Private/Clients/Acme` (three levels deep): every path Bob receives for the subtree is rooted at `Acme`; the folder's metadata carries no parent reference; Bob's `GET` of the parent folder's id returns `404`. Alice's own responses still show full workspace-relative paths.
- **AC-11** (FR-13) A byte-level scan of all of Bob's responses across listing, detail, search, events, and activity surfaces finds no occurrence of `Private`, `Clients`, or their folder ids — the leak test runs the full surface, not one endpoint.
- **AC-12** (FR-4, FR-5) Every child of a folder appears exactly once across cursor-paginated pages at a page size that does not divide the total, subfolders before files; a page is filled across that seam rather than cut short at it; and a cursor reused under a different `sort` is refused.
- **AC-13** (FR-4) A move refused for any reason — the root, a cycle, an occupied name, a name the policy rejects, a folder that is not the caller's — leaves the tree byte-for-byte unchanged, asserted by fingerprinting it before and after all of them.
- **AC-14** (FR-4) A crash between the directory rename and the row update converges on the next scan: every contained file keeps its UUID (matched by content, [F-001/FR-19](F-001-upload-and-import.md)), nothing is trashed and nothing is registered twice. This is why the disk is written before the rows.
- **AC-15** (FR-4, FR-14) A cross-workspace move within one filesystem preserves the contained files' UUIDs and their downloadable content, and reports them under the destination workspace; the same move between filesystems answers `409` naming that reason and leaves both trees untouched.
