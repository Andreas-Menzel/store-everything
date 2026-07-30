# 03 — Storage and Portability

**Status:** Draft
**Related ADRs:** [ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md)

## The core promise

**The app can be removed at any time.** Files live as plain files, in the user's own hierarchical folder structure, on storage the user controls (for v1: a folder — e.g. an external NAS share — mounted into the app, which the app treats as if it were local). If the app disappears tomorrow, the user's data is fully intact and usable: the folders *are* the data.

Consequences:

1. The folder hierarchy the user sees in the app is the real hierarchy on disk (per workspace subtree).
2. The app never rewrites, re-encodes, or "optimizes" originals. Analysis output is stored elsewhere.
3. Everything the app adds lives in two clearly separated places:
   - **PostgreSQL** — domain model, index, tags, metadata, permissions.
   - **Derived store** — previews, keyframes, transcripts, version history (a directory the app owns, distinct from the source tree).
4. All derived data except manual input (tags/confirmations/rejections, users, permissions, shares) is regenerable by reprocessing. Manual input is protected by ordinary DB backups.
5. The organization layer (tags, metadata, detected objects) is **app-private by design** — it exists only in the app's database. Export features (e.g. sidecar files) may come later; the portability promise covers the *files*, while the organization layer's safety net is the DB backup.

## Workspace sources

A workspace corresponds to a **top-level folder** and is defined by *(source type, source location)*:

| Source type | Mutability | Storage |
|---|---|---|
| `local` | read + write | a folder on mounted storage — the folder *is* the workspace storage |
| `external` (GDrive, … — later) | **read-only** through the app: no add/update/delete of files | fully **mirrored** onto the server: every file is copied down; extraction runs on the mirror, which doubles as reliable access if the external service disappears. No writes back, no conflict handling in v1. Details deferred — Q16. |

v1 spec work focuses on `local`; the workspace model carries the `source` field from day one so external backends slot in without redesign.

## Storage layout (proposed)

```
{data-root}/
  users/{user}/
    workspaces/{workspace}/
      .workspace/             ← marker: workspace UUID, source type/config
      data/                   ← the actual file tree (local content, or the
                                mirror of an external source)

/var/lib/store-everything/    ← app-owned (removable without data loss)
  derived/                    ← previews, keyframes, transcripts, renditions
  versions/                   ← shadow copies of superseded file versions and
                                of trashed files' current versions (F-014)
postgres volume               ← database
```

The **database is authoritative** for workspace configuration; the `.workspace` marker only makes a tree re-identifiable after moves/backup-restore — never a second config source that can drift. Folder names are human-readable (you should recognize your data without the app); the UUID lives in the marker.

> Open — Q2: whether `local` workspaces may also *adopt* an existing folder in place (outside `{data-root}` — the NAS-import story with zero copying), and whether renaming a workspace renames its folder on disk.

## Import of existing structures

Users bring an existing folder tree ("we will import the file structure the user currently uses"):

- Point a workspace at an existing subtree → the app scans it, registers every file (path, size, hash, mtime), and queues ingestion. Nothing is moved or renamed.
- Import is resumable and incremental; at 10 TB the initial scan+ingest runs for a long time and must survive restarts.
- Re-scan detects on-disk changes made *outside* the app (files added/modified/deleted directly on the NAS) and reconciles: new file → ingest; changed hash → new version; missing → a trash entry badged "removed outside the app" ([F-014/FR-10](../features/F-014-deletion-and-trash.md)) — never silently purged. Whether this is watch-based, scheduled, or manual is open — Q3.

## Uploads

Uploading through the API writes the file to the target workspace path on the source storage — an upload and a file copied onto the NAS by hand converge to the same state after reconciliation. Uploads support large files (resumable/chunked; 10 TB scale includes multi-GB videos).

## The auto-sort inbox (deferred feature, keep possible)

A special workspace for quick uploads with no chosen destination: files land in an inbox and are automatically organized — initially by simple rules (year/month folders), later optionally by a local AI model that proposes/executes moves. Whether the sorted destination is inside the same workspace or another one is undecided (Q2). Requirement for now: file *move* operations (within/between workspaces, preserving identity, versions, tags) must be first-class API operations, because auto-sorting is just an API consumer doing moves.

## Versioning vs. "the folder is everything" (known tension)

Version history conflicts mildly with pure portability: the current version of every file lives in the source tree, but *superseded* versions cannot (the user would see duplicate stale files). Resolution (ADR-0003):

- **Current version**: always the real file at the real path. Portability promise fully holds for the present state.
- **Superseded versions**: content is preserved in the app-owned `versions/` area, addressed by content hash, with their derived data retained in the DB (so old versions stay searchable — [F-007](../features/F-007-versioning.md)). Unlike everything else the app stores, `versions/` is **not regenerable** — it is mandatory backup scope (Q13).
- **Restorability depends on who wrote** ("option b"): when the *app* mediates a change (API upload; later the laptop sync client turning a local save into a new-version call), it moves the old file into `versions/` before writing — cheap, no copy, fully restorable. A file edited *directly on disk* is overwritten before the app can snapshot it: re-scan records the new version, and the predecessor keeps all its previously extracted derived data — still **searchable**, but marked `restorable: false`. No copy-on-ingest, no doubled storage.
- Deleting the app loses *history*, never *current data*. This is documented, accepted behavior.
- Retention/quota policy for version history is open — Q4.

## Deletion & trash

In-app deletion is **logically instant, physically deferred** ([F-014](../features/F-014-deletion-and-trash.md)): the item's state flips to `trashed` — gone from every listing and every search immediately — and its current-version content is moved into the content-addressed `versions/` area by a background job. Trash reuses the versioning mechanism, so it needs **no separate storage area** and automatically falls inside the mandatory backup scope (Q13). Consequences:

1. **The source tree only ever contains live files.** Someone browsing the workspace over SMB never sees a file the app considers deleted. (During the safeguarding window the file may briefly remain on disk; re-scan knows to ignore it.)
2. **Cost honesty:** "move into `versions/`" is a cheap rename only when `{data-root}` and the app volume share a filesystem — otherwise it is a copy. This applies equally to version snapshots; ops guidance is to colocate `versions/` with the source filesystem where possible. Corner case: on a full app volume a cross-filesystem *trash* move can fail while *purge* always works — the error message must say exactly that ([F-014/FR-2](../features/F-014-deletion-and-trash.md)).
3. **Purge** deletes version blobs reference-counted — content-addressed blobs may be shared across files — and never requires free space ([F-014/FR-7](../features/F-014-deletion-and-trash.md)).
4. Removing the app loses *trash* the same way it loses *history* (content-addressed blobs in the app-owned area) — the same accepted, documented trade-off as versions. Current live data is never affected.

## Integrity

- Every version gets a content hash at ingest; re-scan can verify (bit-rot detection on demand).
- Hash equality is used to skip redundant re-extraction (same bytes → reuse extraction results, still recorded per file).
