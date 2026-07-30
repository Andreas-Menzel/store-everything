# F-016 — Archive Download (Folders & Selections)

**Status:** Draft
**Priority:** P1
**Depends on:** F-008, F-015
**Related specs:** [08-api-principles](../specs/08-api-principles.md), [09-previews](../specs/09-previews.md#storage), [F-012](F-012-live-updates.md) (progress), [F-014](F-014-deletion-and-trash.md) (state)

## Summary

Download a folder — or any selection of files and folders — as a single archive. The server resolves the selection into a **manifest**: the permission-filtered, sorted list of (relative path, version content hash) the *requester* may read. The manifest's hash is the cache key: identical requests re-download instantly, any change in content or visibility yields a new artifact, and users with identical visibility share one cached artifact. Because building a large archive takes time, creation is an async job with progress; because artifacts are frozen once built, downloads are Range-resumable. Authorization is never frozen: every download request re-validates the requester's current permissions against the archive's manifest.

## User stories

- As a user, I want to download a whole folder as a zip so that I don't click through hundreds of files.
- As a user, I want to select several files and folders and download them together.
- As a user on a flaky connection, I want to resume a 40 GB archive download instead of restarting it.
- As a user, I want to see when a previously built archive no longer matches the folder and rebuild it with one action.
- As an owner, I expect someone whose access I revoked to be unable to keep downloading an archive containing my files.

## Functional requirements

- **FR-1** `POST /archives` accepts a selection (file and/or folder ids) and a `format`; downloading one folder is the one-element case. **v1 format: `zip` only** (zip64 mandatory); the parameter exists so more formats can be added without API change.
- **FR-2** **Manifest as cache key:** the selection resolves — at request time, with current permissions — to a sorted manifest of (relative path, version content hash) over readable files, plus readable empty directories. Equal manifest hash → the existing artifact is returned immediately (descriptor); no stale-serving path exists through this endpoint. Users with identical visibility share one artifact.
- **FR-3** **Async build:** on cache miss, `202` + job id; the build runs at interactive priority (P0 — someone is waiting, the on-demand-rendition precedent in [09](../specs/09-previews.md#generation-policy)), reports progress via the job API and [F-012](F-012-live-updates.md), and emits a completion event (future notification hook).
- **FR-4** **Frozen artifact, resumable download:** `GET /archives/{id}` returns the descriptor; `GET /archives/{id}/content` serves bytes with Range support. The artifact is immutable — that is what keeps byte offsets stable for resume.
- **FR-5** **Authorization is re-validated on every download request, including every Range request:** the requester must currently hold `read` on every file in the *stored* manifest (one indexed query — no rebuild, no manifest recomputation). A failed check blocks immediately, even mid-resume. Build-time authorization never outlives the grants it was based on ([F-008/FR-8](F-008-sharing-and-public-links.md) extended to archives).
- **FR-6** **Freshness is displayed, not enforced:** the descriptor compares the stored manifest against a freshly resolved one and reports `current` or `outdated` (with a change count) plus a rebuild action. **An outdated but permission-valid artifact remains downloadable until TTL/eviction, clearly badged** — a content change must not kill an in-flight resume; only permission failure does.
- **FR-7** **Storage:** artifacts live in the derived store as a cache kind with its own size cap and TTL (proposed default 48 h — ops tuning), evicted before other derived kinds under pressure; after eviction the same request simply rebuilds (same manifest → same key).
- **FR-8** **Limits:** archive entries are stored uncompressed by default (media doesn't compress; predictable speed); an admin-configurable ceiling on manifest size (proposed default: 50 GB or 100k entries) rejects oversized selections with a problem response that says how to split; per-user concurrent builds are capped (1–2).
- **FR-9** **Build integrity:** the builder verifies each file's content hash while reading; archives are best-effort snapshots as of build time (documented), and empty directories are included.

## API surface

`POST /archives` (selection + format → descriptor or `202` + job) · `GET /archives/{id}` (descriptor: manifest summary, size, freshness, expiry) · `GET /archives/{id}/content` (Range download) · `GET /jobs/{id}` (build progress).

## Out of scope

Formats beyond zip (parameter reserved). Compression tuning. Archiving search results (later — same selection primitive). Archives via public share links (folder links are deferred in F-008). Archive encryption.

## Open questions

None feature-local — TTL, size cap, and concurrency defaults are ops tuning (Q15 precedent). Symlink handling inside archived trees follows [Q22](../OPEN-QUESTIONS.md).

## Acceptance criteria

- Requesting a folder archive returns `202`, the job completes, the download succeeds and unpacks to exactly the permission-visible tree including empty directories; an immediately repeated identical request returns the same artifact with no new build.
- A download interrupted mid-stream resumes via Range and the completed file's checksum matches an uninterrupted download.
- Revoking `read` on one contained file blocks the next Range request of an in-flight download; re-granting restores downloadability of the same artifact.
- Two users with different visibility over the same folder get different artifacts; a user can never fetch an artifact whose manifest contains a file they cannot currently read — including by guessing artifact ids (leak test).
- After a contained file changes, the old artifact's descriptor reports `outdated` with a rebuild action and remains downloadable until TTL; a new `POST` with the same selection yields a new artifact.
- A selection exceeding the configured ceiling is rejected with the documented problem type; an archive containing a > 4 GB file unpacks correctly (zip64).
- Already-compressed media in an archive is not recompressed (archive size ≈ sum of inputs).
