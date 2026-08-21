# 03 — Storage and Portability

**Status:** Draft
**Related ADRs:** [ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md), [ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md), [ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md), [ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)

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

### Placement: managed and adopted roots

Every `local` workspace has a **root directory** on disk plus a **placement** saying who chose that path ([ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)):

| Placement | Root directory | Created by |
|---|---|---|
| `managed` | `SE_DATA_ROOT/users/{owner-id}/workspaces/{name}/data` — ours to shape | any member, for themselves |
| `adopted` | an existing directory, indexed in place with **nothing moved or copied** | **admin only**, and only inside the `SE_ADOPTION_ROOTS` allow-list |

Everything downstream — scanning, folders, uploads, versions, permissions — treats the two identically. Only two behaviors differ:

- **Creation.** An adopted root is accepted only if it resolves (`realpath`) inside an allow-listed root, is a directory, does not overlap the app-owned areas, neither contains nor is contained by another workspace root, and passes the filesystem probe ([§ filesystem requirements](#filesystem-requirements)). Members never submit filesystem paths.
- **Rename.** Renaming a `managed` workspace renames its directory. An `adopted` root path is **immutable for the workspace's lifetime** — the display name is metadata; re-pointing at another directory is a new workspace, not a rename.

**A workspace is created in two steps**, because the row and the directory cannot commit together ([ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md)). The request validates everything it can — name, placement, allow-list containment, overlap, the filesystem probe — and records the workspace in state `provisioning` together with the operation that will build it. That operation creates the root (managed) or verifies it (adopted), re-probes it, plants the control directory, registers the root folder, and flips the workspace to `active`. Consequences worth stating:

- A refusal is **synchronous**: an allow-list violation, an overlap, or a failed probe is answered by the creating request, naming what failed — nothing is recorded.
- A workspace that is still `provisioning` is not usable and says so; a crash mid-provisioning leaves that state and an operation that will be claimed again, never a row pointing at a directory that does not exist.
- Both the pre-flight verdict and the final one are recorded on the workspace. The pre-flight probes the *filesystem* the root will live on (`SE_DATA_ROOT` for a managed placement, whose directory does not exist yet); the provisioning run probes the **real root**, and its verdict is the one the workspace keeps.

## Storage layout

```
SE_DATA_ROOT/                 ← managed placement (default /srv/store-everything)
  users/{owner-id}/
    workspaces/{name}/
      data/                   ← THE WORKSPACE ROOT: the file tree (local
                                content, or the mirror of an external source)
        .workspace/           ← the one control directory the app plants in a
                                workspace root (reserved name, scan-skipped)
          marker              ← workspace UUID, placement, created-at (JSON)
          staging/            ← write staging (uploads, app-mediated writes),
                                files named by operation id: same filesystem as
                                the destination → finalize is an atomic rename;
                                janitor-collected (12-reliability.md)

/mnt/…/some/existing/tree/    ← adopted placement: THE WORKSPACE ROOT is the
  .workspace/                   user's own directory — same control directory
  …the user's files…            inside it, nothing relocated

/var/lib/store-everything/    ← app-owned (removable without data loss)
  derived/                    ← previews, keyframes, transcripts, renditions
  versions/                   ← shadow copies of superseded file versions and
                                of trashed files' current versions (F-014)
postgres volume               ← database
```

The **database is authoritative** for workspace configuration; the `.workspace/marker` only makes a tree re-identifiable after a move or a backup restore — never a second config source that can drift.

**Which path segments are human-readable, and why not all of them.** The workspace's own directory carries its **name**, so that someone browsing the storage without the app recognizes what they are looking at — and so that renaming a managed workspace is a directory rename, as [ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md) requires. The owner's directory carries their **id**: an email address is neither length-bounded nor guaranteed free of `/` under [08](08-api-principles.md)'s validation, and a display name is not unique — either would turn an account edit into a directory move, or two users into one path. Whose tree a directory is answers from the workspace's own record, not from its path.

`.workspace/` is the **only** thing the app writes into a user's tree, in both placements — the price of atomic renames, which require staging on the destination filesystem. It is a reserved name at the workspace root ([F-015/FR-6](../features/F-015-folders.md)), skipped by every scan, and visible to anyone browsing the tree over SMB; operator documentation names the Samba options that hide it.

The app-owned areas (`derived/`, `versions/`) sit **outside every workspace root** for both placements. An adopted root commonly lives on a different filesystem than the app volume, which makes [12](12-reliability.md#filesystem-write-protocol)'s journaled cross-filesystem move the normal path there rather than an exotic one — see the cost honesty in [§ deletion & trash](#deletion--trash).

## Import of existing structures

Users bring an existing folder tree ("we will import the file structure the user currently uses"):

- Point a workspace at an existing subtree → the app scans it, registers every file (path, size, hash, mtime), and queues ingestion. Nothing is moved or renamed.
- Import is resumable and incremental; at 10 TB the initial scan+ingest runs for a long time and must survive restarts.
- Re-scan detects on-disk changes made *outside* the app (files added/modified/deleted directly on the NAS) and reconciles: new file → ingest; changed hash → new version; missing → a trash entry badged "removed outside the app" ([F-014/FR-10](../features/F-014-deletion-and-trash.md)) — never silently purged.
- Two kinds of entry are **reported instead of registered**: siblings whose names collide on the comparison key ([§ names on disk](#names-on-disk)) and symlinks ([§ symlinks](#symlinks)). Both appear in the scan report; neither is ever resolved by touching the user's files.

### Change detection

Detection has one guaranteed path and two accelerators ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)):

| Mechanism | Role |
|---|---|
| **Scheduled scan** — durable per-workspace `next_due_at`, default hourly, tunable | The correctness backstop. A **stat-scan**: compare size and mtime, hash only what looks changed. |
| **Manual rescan** — workspace or single subtree | The user's "look now" button, and the answer when a watcher cannot exist. |
| **Filesystem watcher**, where the platform supports it | A [lossy doorbell](12-reliability.md#durable-schedules-lossy-doorbells): events debounce into targeted subtree scans. Overflow, failure, or total absence is **not an error** — the scheduled pass reaches the same state. |

No change is ever noticed *only* because a watcher fired, which is what keeps behavior identical on filesystems that deliver no events at all (every SMB/NFS mount). Verifying the content hash of every file is a **separate on-demand integrity pass** ([§ integrity](#integrity)), never part of routine scanning.

## Names on disk

The same tree is written by Linux, macOS and Windows machines, whose name rules disagree. The app's rules ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)):

- Names are **stored exactly as given or found** — case-preserving, byte-preserving.
- Sibling uniqueness is enforced on a **comparison key**: the name, NFC-normalized and case-folded. `Foo.txt` and `foo.txt` cannot be siblings; neither can the NFC and NFD spellings of one name. Path lookups compare keys, never raw strings.
- Names arriving **through the API** are normalized to **NFC** before storage; names found **on disk** are stored verbatim with the key derived from them.
- A rename changing only case, or only normalization, **is a rename**.
- **Collisions found on disk are scan conflicts**, not errors and not merges: the first entry in the deterministic traversal order registers, the rest are listed as conflicts — visible, unregistered, untouched. Resolution is a human renaming something; the app never renames, moves, or deletes a user's file to resolve a collision.
- Limits, explicit so they fail predictably rather than at the filesystem's whim: **255 bytes** per name (UTF-8), **4096 bytes** per workspace-relative path, no `/`, no control characters, and `.workspace` reserved at the workspace root.

## Symlinks

**Never followed** ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)). The scanner does not dereference a symlink, file or directory; the tree behind a symlinked directory is never traversed, so loops cannot arise. Each link is recorded as a skipped entry in the scan report and becomes no domain object, and the API never creates one. Independently of scanning, **every path the app opens is resolved and re-verified to lie inside its workspace root before any byte moves** — a redundant check by design: lexical containment is not containment, as the File Browser CVEs (CVE-2026-54094 and its incomplete fix CVE-2026-55668) demonstrated.

## Filesystem requirements

The write protocol ([12](12-reliability.md#filesystem-write-protocol)) stands on three properties of a workspace root's filesystem: **atomic same-directory rename**, **honest `fsync`** on files *and* directories, and **listings stable enough to traverse deterministically**. These are not assumed:

- An **`fs-check` probe** ships with the app and exercises them directly (`store-everything fs-check <path>`). It runs when a workspace is created or adopted — recording its verdict on the workspace — and on demand. It reports each required property separately, so a refusal names what is missing rather than saying "unsupported".
- A root whose filesystem fails the probe is **refused, naming the property that failed**.
- **v1 supports filesystems local to the app host.** SMB and NFS mounts stay unsupported until the probe passes against that mount with its own options, because on a filesystem that lies about `fsync` or breaks rename atomicity every guarantee in [12](12-reliability.md) is void.

## Uploads

Uploading through the API writes the file to the target workspace path on the source storage — an upload and a file copied onto the NAS by hand converge to the same state after reconciliation. Uploads support large files (resumable; 10 TB scale includes multi-GB videos).

The wire format is the **IETF resumable-upload protocol**, implemented in-app and the only upload path there is ([ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md)) — the same protocol Apple's Photos background-upload extension speaks, which is what makes [F-021](../features/F-021-mobile-auto-upload.md) work on iOS without a second dialect. A one-request upload is the ordinary case; resumption exists when it is needed.

Upload mechanics are crash-safe per [12](12-reliability.md#filesystem-write-protocol): appended bytes accumulate in the workspace's `.workspace/staging/` area — the same filesystem as the destination, so finalizing is an atomic rename — with received bytes tracked in a durable **upload-session row**, fsync'd before each offset is committed. An interrupted upload resumes from the last committed offset; an abandoned one expires and is janitor-collected. Finalize verifies the content hash (the protocol carries no digest of its own), renames into place, and creates the `FileVersion` plus extraction jobs in one transaction.

Four properties of that sentence are load-bearing, and each is asserted by a test rather than assumed:

- **The committed offset is a promise.** Bytes are `fsync`'d *before* it advances, so a crash in between leaves staging **longer** than the offset — and a resume truncates back to the offset before writing. The reverse order would promise bytes a power cut ate, with the client resuming past a hole neither side can see.
- **The session is a row of its own, not an `operation` row.** It carries an offset that every append compares and advances, which wants columns and a guarded update rather than a JSON payload; it obeys the operation-record *discipline* ([ADR-0017](../decisions/ADR-0017-resumable-upload-protocol.md), [12 § what needs an operation record](12-reliability.md#what-needs-an-operation-record)) — durable before any side effect, staged under its own id so the janitor can attribute the bytes, terminal states that make those bytes collectable — but nothing claims or executes it, because the client drives it. An **open** session's staging is never collected however old it looks; the janitor expires the session first, on database time.
- **A completed session keeps its outcome.** It records the file it produced, so an append whose response was lost replays that result instead of registering a second file ([08 § idempotency](08-api-principles.md#conventions-proposed)).
- **Bytes land before rows.** The rename happens, then the `File` and `FileVersion` rows commit ([02 § invariant 8](02-domain-model.md#invariants)). A crash between them leaves a real file at a real path that the next scan adopts — never a row promising content that is not there.

**No interim `104`.** The protocol says a server SHOULD announce the upload resource in a `104 (Upload Resumption Supported)` interim response "unless the server is not capable of sending interim responses", and ASGI has no message for one — so clients learn the upload resource from the `201 Created` that ends the creation request instead. That is conformant and costs one round trip; what it may cost with Apple's background uploader, which treats the 104 as authoritative, is [Q58](../OPEN-QUESTIONS.md) and needs a real device to answer.

**A name collision is refused, not resolved** ([F-001/FR-7](../features/F-001-upload-and-import.md)): uploading to an occupied path answers `409` on the comparison key, so `Report.pdf` collides with `report.pdf`. Uploading a *new version* over an existing file is the explicit, parameterized case that arrives with [F-007](../features/F-007-versioning.md)'s write path.

## The auto-sort inbox (deferred feature, keep possible)

A special workspace for quick uploads with no chosen destination: files land in an inbox and are automatically organized — initially by simple rules (year/month folders), later optionally by a local AI model that proposes/executes moves. Whether the sorted destination is inside the same workspace or another one is undecided (Q55) — both placements can host either answer. Requirement for now: file *move* operations (within/between workspaces, preserving identity, versions, tags) must be first-class API operations, because auto-sorting is just an API consumer doing moves.

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

- Every version gets a **SHA-256** content hash at ingest, stored with its algorithm so a future algorithm is an additive change ([02](02-domain-model.md#fileversion)); re-scan can verify (bit-rot detection on demand).
- Hash equality is used to skip redundant re-extraction (same bytes → reuse extraction results, still recorded per file).
- The admin `verify` audit ([12](12-reliability.md#verification)) extends this to the app-owned areas: every referenced blob exists (with hash spot-checks), every unreferenced blob is younger than the janitor's grace window, version-blob refcounts add up.
