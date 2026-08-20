# ADR-0018 — Workspace layout: managed and adopted roots, one control directory

**Status:** Accepted
**Date:** 2026-08-20

## Context

Two open questions were really one: **where a workspace's files live** (Q2 — may a `local` workspace adopt an existing folder in place, and does renaming it rename the directory?) and **where the app stages its writes** (Q31 — staging must share a filesystem with its destination, so it lands inside the user's tree; what exactly do we plant there, and what ignores it?).

The forces:

- [ADR-0003](ADR-0003-files-on-disk-source-of-truth.md) makes the user's own tree the data. The headline import story is a 10 TB NAS folder adopted **without copying a byte**; a design that requires copying into an app-owned location contradicts the product.
- [ADR-0010](ADR-0010-crash-only-execution-model.md) and [12 § filesystem write protocol](../specs/12-reliability.md#filesystem-write-protocol) require staged writes to be finalized by an **atomic same-filesystem rename**. Staging therefore cannot live on the app volume when the destination is a NAS mount — it must live inside the destination tree.
- **Comparable products converge on the same two answers.** In-place adoption is normal (Immich external libraries, Nextcloud local external storage, PhotoPrism's originals folder) and is *always* admin-configured, never an end-user-supplied path — Nextcloud states it is admin-only "for security". Apps that own their storage layout instead (Paperless-ngx's consuming inbox, Seafile's block store) pay for it in exactly the portability this product promises.
- **Staging litter is a known failure mode.** Everyone stages dot-named temp files in the destination directory; Nextcloud's un-janitored `.part` files scattered through user trees are the documented anti-pattern, and Syncthing's approach — one reserved namespace (`.stfolder`, `.stversions`, `.stignore`), auto-excluded from its own scans and cleaned on a TTL — is the mature one.

## Decision

**One workspace model, two placements, one control directory.**

1. **Placement is a property, not a separate concept.** Every `local` workspace has a root directory on disk plus a `placement`:
   - `managed` — created by us at `{data-root}/users/{user}/workspaces/{workspace}/data`.
   - `adopted` — an existing directory, indexed in place, nothing moved or copied.

   Everything downstream (scanning, folders, uploads, versions) treats the two identically; only creation and rename differ.

2. **Adoption is admin-only and allow-listed.** `SE_ADOPTION_ROOTS` (absolute paths, empty by default — adoption disabled) is the complete set of locations a workspace may adopt. A candidate root is accepted only if it resolves (`realpath`) to a path inside one of those roots, is a directory, does not contain and is not contained by another workspace root, and passes the filesystem probe ([ADR-0019](ADR-0019-source-tree-semantics.md)). Members never submit filesystem paths; an admin creates the workspace and it is owned by exactly one member, as [02](../specs/02-domain-model.md#workspace) already requires.

3. **One control directory per workspace root: `.workspace/`.** It holds:
   - `marker` — workspace UUID, placement, creation time. It exists so a tree stays re-identifiable after a restore or a move. **The database remains authoritative**; the marker is never a second configuration source that can drift.
   - `staging/` — write staging for uploads and every app-mediated write, files named by their operation id, on the destination filesystem so finalizing is an atomic rename.

   `.workspace` is a **reserved name at the workspace root** ([F-015/FR-6](../features/F-015-folders.md)), skipped by every scan, and its staging area is TTL-collected by the janitor ([12](../specs/12-reliability.md#debris--the-janitor)). It is the *only* thing the app writes into a user's tree, and that is a documented cost of atomic renames — including in adopted trees, where the user will see it over SMB (operator documentation names the Samba options that hide it).

4. **Rename semantics follow ownership of the path.** Renaming a `managed` workspace renames its directory, because the path is ours to shape. An `adopted` workspace's root path is **immutable for its lifetime** — the display name is metadata only. Re-pointing an adopted workspace at a different directory is not a rename; it is a new workspace.

5. **The app-owned areas stay outside every workspace root**, for both placements: `versions/` and the derived store live under `/var/lib/store-everything/` ([03 § storage layout](../specs/03-storage-and-portability.md#storage-layout)).

## Consequences

- A 10 TB NAS tree is adopted by indexing it, with zero copying, which is what the product promised.
- **Adoption's blast radius is an operator decision.** Path traversal, symlink escapes, and "which mount is this inside the container" mistakes are bounded by an env allow-list plus resolved-path containment checks rather than by trusting a request field. The container-vs-host path confusion that Immich documents is ours to document too.
- The app plants exactly one directory in a user's tree. It is discoverable, documented, hideable over SMB, and cleaned on a schedule — the failure mode we deliberately avoid is scattered orphan temp files with no janitor.
- **`versions/` and the derived store may sit on a different filesystem than an adopted root**, so [12](../specs/12-reliability.md#filesystem-write-protocol)'s journaled cross-filesystem move is the *normal* path for adopted trees, not an exotic one — and the cost honesty in [03 § deletion & trash](../specs/03-storage-and-portability.md#deletion--trash) ("a cheap rename only when they share a filesystem") applies to most adopted deployments. Operator guidance is to colocate them where possible.
- Moving a workspace between placements would mean copying its content, which v1 does not offer.
- Where the auto-sort inbox's sorted output lives stays open (Q55) — it is a question about [F-010](../features/F-010-auto-sort-inbox.md)'s destinations, not about this layout, and both placements can host either answer.
- Per-workspace quotas and settings remain unspecified (Q57); nothing here precludes them.
