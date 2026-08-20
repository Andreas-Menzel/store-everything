# ADR-0019 — Source-tree semantics: names, symlinks, change detection, filesystem requirements

**Status:** Accepted
**Date:** 2026-08-20

## Context

[ADR-0003](ADR-0003-files-on-disk-source-of-truth.md) makes filesystem semantics leak into the app **by design**: the tree is the data, other programs write to it, and the same tree is browsed over SMB by macOS and Windows machines whose name rules differ from Linux's. Four open questions were all instances of the same problem — what exactly does the app assume about the directories it indexes? — and they block implementation together:

- **Q25** name case policy (explicitly blocks [F-015/FR-6](../features/F-015-folders.md)),
- **Q22** symlinks in source trees,
- **Q3** external change detection (watchers vs. scheduled scans),
- **Q32** which filesystem guarantees the write protocol may rely on.

What comparable products teach:

- **Names.** Dropbox and OneDrive are case-insensitive and case-preserving; Google Drive permits same-name siblings, which is why rclone ships an entire `dedupe` command to clean up after it; Nextcloud passes Linux case-sensitivity straight through and has carried unresolvable Windows/macOS client clashes for a decade. Unicode is the same story: Syncthing normalizes names during scan (`autoNormalize`, default on) because macOS's NFD and everyone else's NFC otherwise produce two files with one visible name, while Nextcloud normalizes only on its own upload paths and NFD names arriving on local external storage become inaccessible.
- **Symlinks.** The consensus is unanimous — never follow them. Syncthing represents links as inert objects and never dereferences; Nextcloud refuses to follow them on local storage and hides the escape hatch behind a global flag documented as a security risk; Immich and Paperless-ngx skip them. File Browser's CVE-2026-54094 is the precise failure this prevents: per-user scoping validated paths *lexically*, so a link whose path was inside the scope but whose target was outside crossed the boundary — and its first fix was itself incomplete (CVE-2026-55668, dangling links validated by parent directory rather than target).
- **Change detection.** Nobody trusts watchers alone. Syncthing runs a watcher *and* a full scan every hour; Immich scans nightly and labels its watcher experimental, having removed its polling mode as unmaintainable; Paperless-ngx tells users to switch to polling on NFS/SMB; Nextcloud's answer is cron plus `occ files:scan`. Kernel watch events categorically do not fire for changes made on the server side of an SMB/NFS mount.

## Decision

Four rules govern every directory the app indexes.

### 1. Names: stored as found, unique by comparison key

- Names are **stored exactly as given or found** (case-preserving, byte-preserving).
- Sibling uniqueness is enforced on a **comparison key** = NFC-normalized, case-folded name. Two siblings may not share a key: `Foo.txt` and `foo.txt` cannot coexist in one folder, and neither can the NFC and NFD spellings of the same name.
- Names arriving **through the API** are normalized to **NFC** before storage. Names found **on disk** are stored verbatim, with the key derived from them.
- A rename that changes only case or only normalization **is a rename**, not a no-op and not a conflict.
- **Import collisions are reported, never resolved on disk.** When a scan finds siblings colliding on their key, the first in the deterministic traversal order registers and the others are recorded as **scan conflicts**: visible, unregistered, and untouched — the app never renames, moves, or deletes a user's file to fix a collision.
- Hard limits, explicit because the filesystem's would surface as random failures: **255 bytes** per name (UTF-8), **4096 bytes** per workspace-relative path, no `/` and no control characters, and `.workspace` reserved at the workspace root ([ADR-0018](ADR-0018-workspace-layout-and-adoption.md)).

### 2. Symlinks are never followed, and containment is re-verified at open time

- The scanner **does not dereference symlinks**, whether they point at files or directories. Each one is recorded as a **skipped entry in the scan report** and becomes no domain object; the tree behind a symlinked directory is never traversed.
- The API never creates a symlink.
- Independently of the scanner, **every path the app opens for reading or writing is resolved and re-verified to lie inside its workspace root** before any byte moves. The File Browser CVEs are the reason this is a separate, redundant check rather than a consequence of the scan: lexical containment is not containment, and a dangling link must fail the check too.

### 3. Change detection: the scheduled scan is the truth, the watcher is an accelerator

- Every workspace carries a durable scan schedule (`next_due_at` — [12](../specs/12-reliability.md#durable-schedules-lossy-doorbells)), **default hourly**, per-workspace tunable. A scheduled pass is a **stat-scan**: compare size and mtime, and compute a content hash only for entries that look changed.
- **Manual rescan** is a first-class operation, for a whole workspace or a single subtree.
- Where the filesystem supports it, a **watcher** debounces events into targeted subtree scans. It is a lossy doorbell in the [12](../specs/12-reliability.md#durable-schedules-lossy-doorbells) sense: watcher absence, overflow, or failure is **not an error condition**, because the scheduled pass reaches the same state. No watcher event is ever the only reason a change gets noticed.
- **Full-hash verification of every file is a separate on-demand integrity pass** ([03 § integrity](../specs/03-storage-and-portability.md#integrity)), never part of routine scanning — re-hashing 10 TB hourly is not a change-detection strategy.

### 4. Filesystem guarantees are checked, not assumed

The write protocol requires three properties of a workspace root's filesystem: **atomic same-directory rename**, **honest `fsync`** on files and on directories, and **directory listings stable enough to traverse deterministically**.

- An **`fs-check` probe ships with the app**, exercising those properties directly. It runs when a workspace is created or adopted, records its verdict on the workspace, and is runnable on demand.
- A root whose filesystem fails the probe is **refused, naming the property that failed**.
- **v1's supported configuration is a filesystem local to the app host.** SMB and NFS mounts are not supported until the probe passes against that specific mount with its specific options — the probe is what turns Q32 from a promise into a check.

## Consequences

- SMB clients cannot produce the phantom-duplicate states Nextcloud documents: a macOS client writing an NFD name into a tree that already holds its NFC spelling collides on the key and is reported rather than silently doubled.
- A Linux tree containing case-colliding siblings **imports partially and says so**. That is a new user-visible surface we now owe: scan conflicts must be listable and explicable, and the user resolves them by renaming on disk (or through the API), never the app on its own.
- Case-insensitive uniqueness costs a functional index on the comparison key, and path lookups compare keys instead of raw strings — a discipline every query must follow, not an optimization.
- Symlink-heavy trees register **fewer files than `find` reports**. The scan report is the evidence, and the number is explainable rather than mysterious.
- Because the watcher is optional, the same code path runs on every filesystem, CI needs no inotify, and losing watch capacity degrades latency instead of correctness. The hourly default means an external change surfaces within an hour unless the watcher or a manual rescan gets there first.
- **Refusing unproven filesystems will annoy NAS users**, and that is deliberate: on a mount that lies about `fsync` or breaks rename atomicity, every guarantee in [12](../specs/12-reliability.md) is void, and silent data loss is worse than a refused workspace. Extending support is empirical — run the probe, then widen the supported set.
