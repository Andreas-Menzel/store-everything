# Implementation Roadmap

**Current phase:** 0 — Foundations & toolchain *(in progress — no feature code exists yet)*

The ordering authority for implementation. Each phase delivers **one clear new segment of the app**, is independently testable, and lists everything needed to work it: the features it delivers, the specs/ADRs to read first, the open questions to answer at entry, and the exit criteria that close it. This file records **order and rationale, not schedule** — dates are deliberately absent.

This file never records status. Feature statuses live in [features/README.md](features/README.md) (`Implemented` is computed by the [traceability matrix](specs/11-engineering-standards.md#requirement-traceability-the-matrix)); question statuses live in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

## Working a phase

1. **Read everything the phase links** — feature files, specs, ADRs — before planning anything ([CLAUDE.md](CLAUDE.md) workflow, step 1).
2. **Answer the gate questions** at phase entry: each resolution lands as an ADR or spec update and marks its row in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md); newly deferred decisions become new rows.
3. **Plan each feature in detail** (proposal → explicit approval → spec/feature-file updates), then **implement feature by feature in the listed build order** — every feature is its own independently verifiable delivery: its FRs verified by their declared methods, its acceptance criteria running unattended in CI.
4. **Close the phase** when the exit criteria hold honestly: tag a release, move the *Current phase* marker.

## Maintenance rules

1. **Every feature has exactly one home** — a phase or the [post-v1 pool](#post-v1-pool). A new or re-scoped feature file is assigned here **in the same change**.
2. **Reordering is an edit to this file** with its rationale in the change (plus an ADR / OPEN-QUESTIONS row when it reflects a real decision).
3. **Gate lists record what gated the phase.** They change when scope changes — not when a question resolves; [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) keeps question status.
4. **Staged features name their split** (which part lands in which phase). When a feature file's FRs change, its roadmap references are checked in the same change.
5. **Phase numbers are stable once a phase has begun**; unstarted phases may be re-cut freely.
6. **Pulling a pool item forward** means assigning it a phase — and writing its feature file first if it has none.

## Ordering principles

1. **Dependency edges first** — the `Depends on:` headers of the feature files are never violated.
2. **Substrate before rider.** Cross-cutting mechanisms that every write path touches — the transactional event log ([ADR-0007](decisions/ADR-0007-unified-event-log.md)), crash-only operation records ([ADR-0010](decisions/ADR-0010-crash-only-execution-model.md)), permission-aware query construction ([06](specs/06-search.md#permission-aware-by-construction)), provenance/generation stamping ([ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md)) — are built before the features that ride them. Retrofitting any of these means rewriting every write path.
3. **Every phase ends dogfoodable**, extending one E2E spine: upload → extract → search → share.
4. **P0 before P1 before P2** wherever dependencies allow.
5. **Decisions are batched at phase entry** — each phase names the OPEN-QUESTIONS rows that must be answered before its implementation starts.
6. **API before UI within a phase; web before mobile across phases.** The web UI is the baseline client ([F-025](features/F-025-client-parity.md)); the mobile apps follow once the API they consume exists.
7. **Test infrastructure arrives with the thing it tests**: the traceability matrix with the first feature PR, the conformance kit with the extractor contract, the golden-query benchmark with search. Every phase exit is tagged — the upgrade-path drill ([11](specs/11-engineering-standards.md#ci-pipeline-the-enforcement-list)) needs a previous tagged release to exist.

## Overview

| Phase | New segment | Features | Answer at entry |
|---|---|---|---|
| [0](#phase-0--foundations--toolchain) | Foundations & toolchain | — (infrastructure only) | Q10, Q34, Q26, Q28 (v0), ADR-0001 acceptance |
| [1](#phase-1--files--identity) | Files & identity | [F-001](features/F-001-upload-and-import.md), [F-015](features/F-015-folders.md) *(+ F-007 write path, F-011 log)* | Q38, Q31, Q32, Q2, Q25, Q22, Q3, Q30 |
| [2](#phase-2--extraction-platform--tagging) | Extraction platform & tagging | [F-004](features/F-004-document-text-extraction.md), [F-003](features/F-003-tagging.md) *(+ F-009 generations schema, previews/thumbnails)* | Q5, Q7, Q42, Q9 (documents/OCR part) |
| [3](#phase-3--search--library) | Search & library | [F-002](features/F-002-hybrid-search.md), [F-005](features/F-005-image-analysis.md), [F-006](features/F-006-av-transcription-and-keyframes.md), [F-017](features/F-017-views.md) *(sans map)* | Q8, Q27, Q9 (embeddings/vision/speech), Q14 |
| [4](#phase-4--multi-user--data-safety) | Multi-user & data safety | [F-008](features/F-008-sharing-and-public-links.md), [F-014](features/F-014-deletion-and-trash.md) *(+ complete F-007, F-011)* | Q4, Q21, Q12 |
| [5](#phase-5--live--complete-web) | Live & complete web (**web v1**) | [F-012](features/F-012-live-updates.md), [F-013](features/F-013-duplicate-detection.md), [F-016](features/F-016-archive-download.md) *(+ complete F-009, F-017 map)* | Q18, Q17, Q33, Q13, Q20, Q35 |
| [6](#phase-6--mobile-foundation) | Mobile foundation (read path) | [F-019](features/F-019-mobile-connection.md), [F-020](features/F-020-mobile-library.md), [F-026](features/F-026-offline-cache-and-prefetch.md) | Q40, Q46, Q45, Q48, Q49 |
| [7](#phase-7--mobile-backup--parity--v10) | Mobile backup & parity (**v1.0**) | [F-021](features/F-021-mobile-auto-upload.md), [F-022](features/F-022-device-storage-reclaim.md), [F-024](features/F-024-offline-files-and-downloads.md), [F-025](features/F-025-client-parity.md) | Q44 |
| [pool](#post-v1-pool) | Post-v1 | [F-010](features/F-010-auto-sort-inbox.md), [F-018](features/F-018-people.md), [F-023](features/F-023-os-file-manager-integration.md), … | — |

**Staged features** (split across phases, per maintenance rule 4): [F-007](features/F-007-versioning.md) (1 → 4), [F-011](features/F-011-audit-trail.md) (1 → 4), [F-009](features/F-009-reprocessing.md) (2 → 5), [F-017](features/F-017-views.md) (3 → 5). A staged feature computes `Implemented` only when its last part lands.

---

## Phase 0 — Foundations & toolchain

**New segment:** a deployable, empty, fully-gated skeleton — `compose up` on a clean server serves an authenticated API behind Traefik, and CI enforces every standard from day one. No features; everything after this phase is feature work on rails.

**Build order:**

1. **Stack decisions** — resolve the gate questions below; write the resulting ADRs; accept or supersede [ADR-0001](decisions/ADR-0001-postgresql-single-datastore.md).
2. **Repository scaffolding** — core service, migration tooling (up **and** down in CI), OpenAPI toolchain with generated client ([08](specs/08-api-principles.md)), lint/format/type gates.
3. **CI enforcement list** from [11](specs/11-engineering-standards.md#ci-pipeline-the-enforcement-list) — including the traceability-matrix tooling (marker convention + script; "needed from the first feature PR"), coverage ratchet, commit-format check, secret scan, SBOM.
4. **Test infrastructure v0** — ground-truth corpus starter with machine-readable manifest ([11 § test infrastructure](specs/11-engineering-standards.md#test-infrastructure)), fault-injection harness skeleton ([12 § verification](specs/12-reliability.md#verification)).
5. **Deployment skeleton** — Docker Compose behind the external Traefik, health/readiness endpoints, 12-factor config ([10](specs/10-deployment-and-operations.md)); `make release` (Conventional-Commits-derived SemVer, [11 § versioning](specs/11-engineering-standards.md#versioning--releases)).

**Read first**
- **Features:** —
- **Specs:** [00-vision-and-goals](specs/00-vision-and-goals.md) · [01-architecture](specs/01-architecture.md) · [08-api-principles](specs/08-api-principles.md) · [10-deployment-and-operations](specs/10-deployment-and-operations.md) · [11-engineering-standards](specs/11-engineering-standards.md)
- **ADRs:** [ADR-0001](decisions/ADR-0001-postgresql-single-datastore.md) · [ADR-0005](decisions/ADR-0005-single-server-docker-network.md) · [ADR-0009](decisions/ADR-0009-external-traefik-edge.md) · [ADR-0010](decisions/ADR-0010-crash-only-execution-model.md)

**Answer at entry** ([OPEN-QUESTIONS.md](OPEN-QUESTIONS.md)):
- **Q10** — core service language/framework (blocks all code; selection criteria include the job-queue story)
- **Q34** — job-queue library vs. hand-rolled (couples to Q10)
- **Q26** — frontend stack & shared-UI tooling (the first UI increment comes in phase 1)
- **Q28** — test-corpus sourcing & licensing (enough to build corpus v0; grows every phase)

**Exit criteria:**
- `compose up` on a clean host yields a healthy instance behind Traefik; `/health`/readiness honest.
- Every CI gate from the [enforcement list](specs/11-engineering-standards.md#ci-pipeline-the-enforcement-list) that can exist pre-features is live and demonstrably fails on a violating sample (red-test the gates once).
- Traceability-matrix tooling runs (empty matrix); corpus v0 exists with manifest; a migration runs up and down in CI.
- First tagged release exists.

---

## Phase 1 — Files & identity

**New segment:** a single-user personal cloud without intelligence — accounts exist, real files get in (upload, import, re-scan), live in real folders, and come back out, with nothing ever silently lost. The reliability substrate and event log that every later feature rides are in place.

**Build order:**

1. **Identity core** ([07](specs/07-identity-permissions-sharing.md)) — accounts, sessions, personal access tokens, admin bootstrap, abuse protection. (No grants/sharing yet — that is [F-008](features/F-008-sharing-and-public-links.md), phase 4; ownership is the only permission.)
2. **Reliability substrate** ([12](specs/12-reliability.md), [ADR-0010](decisions/ADR-0010-crash-only-execution-model.md)) — operation records, leases + fencing, the filesystem write protocol, janitor, durable schedules. The fault-injection harness becomes real here.
3. **Event log** ([ADR-0007](decisions/ADR-0007-unified-event-log.md)) — transactional outbox; **every mutation from the first one is logged** ([F-011](features/F-011-audit-trail.md)'s log — its query API/UI follows in phase 4).
4. **Storage layout** ([03](specs/03-storage-and-portability.md)) — workspace trees, staging area, content-addressed `versions/` shadow area.
5. **[F-001](features/F-001-upload-and-import.md) — upload & import**: resumable upload protocol (per Q38), workspace import/adoption, re-scan detecting external add/modify/delete.
6. **[F-015](features/F-015-folders.md) — folders**: UUID identity surviving rename/move, reconciliation, aggregates; **move/rename as first-class operations** (this is deferred [F-010](features/F-010-auto-sort-inbox.md)'s v1 obligation FR-1; FR-3's workspace-model flexibility is a design constraint here too).
7. **[F-007](features/F-007-versioning.md) — write path only** *(staged)*: a new upload to an existing path or a changed file on re-scan preserves the previous content in `versions/` — no data-loss window, ever. Version browsing/restore/search and retention land in phase 4.
8. **Web UI shell** (Vue 3 SPA — [ADR-0014](decisions/ADR-0014-vue-frontend-stack.md)): login, browse, upload, download — plus the **authenticated interactive API docs page** ([08](specs/08-api-principles.md)), which waits for login to exist because the schema endpoint is never public.

**Read first**
- **Features:** [F-001](features/F-001-upload-and-import.md) · [F-015](features/F-015-folders.md) · [F-007](features/F-007-versioning.md) (write-path part) · [F-011](features/F-011-audit-trail.md) (log part) · [F-010](features/F-010-auto-sort-inbox.md) (v1 obligations only)
- **Specs:** [02-domain-model](specs/02-domain-model.md) · [03-storage-and-portability](specs/03-storage-and-portability.md) · [04-ingestion-pipeline](specs/04-ingestion-pipeline.md) (detection stage) · [07-identity-permissions-sharing](specs/07-identity-permissions-sharing.md) · [08-api-principles](specs/08-api-principles.md) · [12-reliability](specs/12-reliability.md)
- **ADRs:** [ADR-0003](decisions/ADR-0003-files-on-disk-source-of-truth.md) · [ADR-0007](decisions/ADR-0007-unified-event-log.md) · [ADR-0010](decisions/ADR-0010-crash-only-execution-model.md)

**Answer at entry:**
- **Q38** — upload wire protocol (explicitly blocks F-001; IETF resumable-uploads unlocks the iOS background-upload extension later)
- **Q31** — staging placement & scan-ignore mechanics
- **Q32** — NAS filesystem guarantees (run the rename/fsync test matrix on the real target hardware)
- **Q2** — workspace layout on disk; adoption-in-place; rename semantics
- **Q25** — file/folder name case policy (blocks F-015 implementation)
- **Q22** — symlinks in source trees
- **Q3** — external change detection (re-scan mechanics: scheduled + manual trigger vs. watchers)
- **Q30** — reliability tuning defaults (ship the spec's conservative defaults; revisit in phase 2 against real extractor runtimes)

**Exit criteria:**
- E2E: create user → log in → create workspace → resumable upload (interrupt + resume) → import an existing subtree → re-scan picks up an external add, modify, and delete → browse folders → download bytes (Range supported).
- Fault-injection green: importer killed mid-run converges with no debris and no duplicated effects; the [12 § verification](specs/12-reliability.md#verification) audit runs clean.
- Overwriting content (re-upload to a path, changed file on re-scan) preserves the prior bytes in `versions/` — negative test proves nothing is lost.
- Every mutation has its event row, written in the same transaction ([02 § invariants](specs/02-domain-model.md#invariants)).
- PATs are listed and revocable; every endpoint rejects unauthenticated calls.
- F-001 and F-015 FRs verified by their declared methods (matrix green for both); phase tagged.

---

## Phase 2 — Extraction platform & tagging

**New segment:** the system starts *understanding* files. The pluggable-extractor platform runs (contract, orchestration, conformance kit), documents yield positioned text, files get thumbnails, and the tag model (manual + auto, provenance) exists — everything a search can later index.

**Build order:**

1. **Extractor contract finalized** ([05](specs/05-extractor-contract.md), [ADR-0002](decisions/ADR-0002-extractor-containers-fixed-api.md)) — wire format (Q5), sandbox enforcement (Q7 — security-first precondition before any extractor runs).
2. **Orchestrator** ([04](specs/04-ingestion-pipeline.md)) — routing by manifest, execution with leases, persistence, priority classes ([04 § prioritization](specs/04-ingestion-pipeline.md#prioritization--scheduling)); **provenance + generation columns on every extraction write from the first result** ([ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md)) — this is [F-009](features/F-009-reprocessing.md)'s schema *(staged; management surface in phase 5)*.
3. **Conformance kit + reference extractor** ([11 § test infrastructure](specs/11-engineering-standards.md#test-infrastructure)) — runnable against any extractor image; the reference extractor doubles as E2E test double.
4. **[F-003](features/F-003-tagging.md) — tagging**: tag DAG ([ADR-0006](decisions/ADR-0006-hierarchical-tags-dag.md)), provenance state machine ([ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md)), manual tagging UI, auto-tag write path (exercised via the reference extractor now; the first real auto-tagger is [F-005](features/F-005-image-analysis.md), phase 3).
5. **`preview-gen` + thumbnails** ([09](specs/09-previews.md), [ADR-0008](decisions/ADR-0008-renditions.md)) — thumbnail tiers (Q42), preview descriptor, on-demand rendition policy.
6. **Metadata extraction** — EXIF/dates/typed metadata ([02 § MetadataEntry](specs/02-domain-model.md)); feeds phase 3's timeline and date facets.
7. **[F-004](features/F-004-document-text-extraction.md) — document text**: `pdf-text` decision tree, `tesseract-ocr`, plain text/markdown/office/code — segments with page/line anchors, originals never modified.

**Read first**
- **Features:** [F-004](features/F-004-document-text-extraction.md) · [F-003](features/F-003-tagging.md) · [F-009](features/F-009-reprocessing.md) (generations schema part)
- **Specs:** [04-ingestion-pipeline](specs/04-ingestion-pipeline.md) · [05-extractor-contract](specs/05-extractor-contract.md) · [09-previews](specs/09-previews.md) · [02-domain-model](specs/02-domain-model.md) (Tag/FileTag, MetadataEntry, Segment, DerivedAsset, Extractor/ExtractionRun)
- **ADRs:** [ADR-0002](decisions/ADR-0002-extractor-containers-fixed-api.md) · [ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md) · [ADR-0006](decisions/ADR-0006-hierarchical-tags-dag.md) · [ADR-0008](decisions/ADR-0008-renditions.md)

**Answer at entry:**
- **Q5** — extractor dispatch mechanics & exact wire format (explicitly blocks contract finalization)
- **Q7** — sandbox enforcement: no-outbound-network policy, mount scope, admin visibility
- **Q42** — thumbnail 512 px tier (decide before thumbnails ship at scale)
- **Q9** *(partial)* — OCR/document-extraction tooling choices; embedding/vision/speech models wait for phase 3

**Exit criteria:**
- Scanned PDF → OCR segments with page anchors; born-digital PDF → text-layer segments (no OCR run); office/text/code → anchored segments — verified against corpus fixtures.
- Conformance kit green against every official extractor image *and* the reference extractor; a deliberately broken image fails it.
- Sandbox negative test: a default extractor container cannot reach the outside network.
- Images and PDFs have thumbnails at the fixed tier set; heavy renditions generate on demand and cache per policy.
- Tag DAG with query-time expansion works; `manual`/`confirmed` survive a re-run, `rejected` suppresses re-adding ([ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md) state machine tests).
- F-003 and F-004 matrix green; phase tagged.

---

## Phase 3 — Search & library

**New segment:** the product moment — both headline queries from the [README](README.md) work: exact search returns files *with positions* (pages, timestamps), semantic search finds "photo of my dog at the beach". The library (views, tabs, timeline) makes it browsable.

**Build order:**

1. **Embedding infrastructure** ([06 § embedding spaces](specs/06-search.md), [ADR-0001](decisions/ADR-0001-postgresql-single-datastore.md) pgvector/HNSW) + text-embedding extractor (model per Q9).
2. **[F-002](features/F-002-hybrid-search.md) — hybrid search**: exact/FTS over segments, names, typed metadata, tags (with DAG expansion — [ADR-0006](decisions/ADR-0006-hierarchical-tags-dag.md)); filters, facets, pagination; positions + snippets; date histogram + compact projection (FR-19/20 — the timeline backbone); geo filters + grid aggregation (FR-17/18). **Permission-aware by construction from the first query** ([06](specs/06-search.md#permission-aware-by-construction), [07 § search](specs/07-identity-permissions-sharing.md)) — the universe is owner-only until phase 4, but the filter join exists now. Conditional requests (ETags) and version-pinned thumbnail URLs land with the listing endpoints ([08 § conventions](specs/08-api-principles.md), [14](specs/14-client-sync-and-caching.md)) so phase 6's client cache costs the server nothing.
3. **[F-005](features/F-005-image-analysis.md) — image analysis**: objects/scene/OCR/CLIP extractors → first real `auto` tags + semantic image search.
4. **[F-006](features/F-006-av-transcription-and-keyframes.md) — A/V**: Whisper-class transcription with timestamps; keyframes chained through the image pipeline — completes "video Y at 04:12".
5. **Ranking fusion + benchmark**: RRF with exact-beats-semantic; golden-query benchmark (Q8) on reference targets (Q27) wired as the scheduled, release-gating `benchmark` suite ([11 § verification methods](specs/11-engineering-standards.md#verification-methods-per-fr)).
6. **[F-017](features/F-017-views.md) — views & library** *(staged — map page waits for Q35, phase 5)*: view entity, system views seeded (Timeline, Images, Videos, Audio, Documents, Recent), user views, web library UI with timeline.

**Read first**
- **Features:** [F-002](features/F-002-hybrid-search.md) · [F-005](features/F-005-image-analysis.md) · [F-006](features/F-006-av-transcription-and-keyframes.md) · [F-017](features/F-017-views.md)
- **Specs:** [06-search](specs/06-search.md) · [02-domain-model](specs/02-domain-model.md) (Segment, Embedding, View) · [04-ingestion-pipeline](specs/04-ingestion-pipeline.md) (identification/media class, chaining) · [05-extractor-contract](specs/05-extractor-contract.md) · [09-previews](specs/09-previews.md) (keyframes) · [08-api-principles](specs/08-api-principles.md)
- **ADRs:** [ADR-0001](decisions/ADR-0001-postgresql-single-datastore.md) · [ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md) · [ADR-0006](decisions/ADR-0006-hierarchical-tags-dag.md)

**Answer at entry:**
- **Q9** *(remaining)* — text-embedding, CLIP-class, Whisper-size, detector models: CPU-baseline feasibility, licensing for redistribution
- **Q8** — ranking benchmark: query set, metrics, thresholds, cadence
- **Q27** — target scale & reference hardware (blocks meaningful `benchmark` thresholds, e.g. [F-002/FR-10](features/F-002-hybrid-search.md))
- **Q14** — multi-language search UX (German + English at minimum)

**Exit criteria:**
- Corpus-verified: a known phrase returns the document with correct page positions *and* the video containing it spoken at the correct timestamp; a semantic image query returns the expected fixture in top-k.
- Benchmark suite runs scheduled with recorded baseline and thresholds; search p95 meets [F-002/FR-10](features/F-002-hybrid-search.md) on reference hardware *(verify: benchmark)*.
- Timeline histogram + compact projection serve a 100k-item library in one request each; geo filter + grid aggregation work (map *page* still absent).
- Every search path joins the permission filter (query-plan/leak test proves no unfiltered path exists).
- System views seeded and admin-manageable; user views CRUD; library tabs live in the web UI.
- F-002, F-005, F-006 matrix green; F-017 green except map-page FRs (staged); phase tagged.

---

## Phase 4 — Multi-user & data safety

**New segment:** the household arrives — grants, "Shared with me", public links, full version history, and trash with the never-early-purge promise. Permission-aware search is now proven against real grants, and the audit trail becomes visible.

**Build order:**

1. **[F-008](features/F-008-sharing-and-public-links.md) — permissions, sharing & public links**: `read`/`write`/`manage` grants on workspaces/subtrees/files, visibility roots ([07 § visibility roots](specs/07-identity-permissions-sharing.md#visibility-roots-what-a-grantee-sees)), Shared-with-me page, public share links (expiry, password, revocation).
2. **Permission-aware search hardening**: the phase-3 construction meets real grants — exhaustive negative suite (results, snippets, facets, counts, autocomplete) per [11 § what must be tested](specs/11-engineering-standards.md#what-must-be-tested).
3. **[F-007](features/F-007-versioning.md) — completed**: version listing/restore, version search on request, retention policy (Q4). The write path has existed since phase 1.
4. **[F-014](features/F-014-deletion-and-trash.md) — deletion & trash**: live → trashed → purged lifecycle, trash page, janitor purge after retention, external-deletion capture, share-link suspension (Q21), disk-pressure alerting — never silent early purge ([10 § disk space](specs/10-deployment-and-operations.md#disk-space)).
5. **[F-011](features/F-011-audit-trail.md) — completed**: query API + admin UI over the event log that has been written since phase 1.

**Read first**
- **Features:** [F-008](features/F-008-sharing-and-public-links.md) · [F-007](features/F-007-versioning.md) · [F-014](features/F-014-deletion-and-trash.md) · [F-011](features/F-011-audit-trail.md)
- **Specs:** [07-identity-permissions-sharing](specs/07-identity-permissions-sharing.md) · [03-storage-and-portability](specs/03-storage-and-portability.md) (versioning, deletion & trash) · [06-search](specs/06-search.md) (version scope, lifecycle scope, permission filtering) · [02-domain-model](specs/02-domain-model.md) (FileVersion, Permission/ShareLink, Event) · [10-deployment-and-operations](specs/10-deployment-and-operations.md) (disk space)
- **ADRs:** [ADR-0003](decisions/ADR-0003-files-on-disk-source-of-truth.md) · [ADR-0007](decisions/ADR-0007-unified-event-log.md)

**Answer at entry:**
- **Q4** — version-history retention defaults (count/age/size caps, quota interaction, who may purge)
- **Q21** — share links to trashed files: 404 or 410 (decide before share-link serving)
- **Q12** — upload/file-request links: in or out of F-008's v1 scope

**Exit criteria:**
- Two real users: grant → grantee sees the root (nothing of its location leaks) → revoke → gone from browse *and* search within the consistency promise.
- Public link works account-less with expiry/password/revocation; link scope isolation tested.
- Negative leak suite green: Bob never sees Alice's results, snippets, facets, counts, or autocomplete entries; trashed items appear in **no** default surface including semantic-only queries ([02 § invariants](specs/02-domain-model.md#invariants) #7).
- Version restore round-trips; purge leaves zero domain rows and zero bytes, honoring version-blob refcounts; only the event log remembers.
- Audit UI answers "who did what to this file, when" for every mutation type since phase 1.
- F-007, F-008, F-011, F-014 matrix green; phase tagged.

---

## Phase 5 — Live & complete web

**New segment:** the web product is whole and operable — every surface updates live, duplicates/archives/reprocessing round out the feature set, the map page lands, and the operational story (backups, restore, upgrades) is proven. **Exit = web v1.**

**Build order:**

1. **[F-012](features/F-012-live-updates.md) — live updates**: WS auth ticket (Q18), thin permission-routed coalesced notifications, `/events` cursor catch-up feed (also satisfies deferred [F-010](features/F-010-auto-sort-inbox.md)'s obligation FR-2), lossy-doorbell pattern ([12 § durable schedules, lossy doorbells](specs/12-reliability.md)).
2. **[F-013](features/F-013-duplicate-detection.md) — duplicates**: query-time exact-hash groups, review page with bulk actions, per-file note; permission-scoped by construction.
3. **[F-016](features/F-016-archive-download.md) — archive download**: manifest resolution + hash as cache key, async build with progress (via F-012), Range-resumable artifacts, per-request permission re-validation.
4. **[F-009](features/F-009-reprocessing.md) — completed**: selective re-runs (extractor/type/workspace), generation replace with rollback, admin scheduling surface — concurrency caps + pause/resume (Q17).
5. **[F-017](features/F-017-views.md) — map page** (Q35 tile default) riding the phase-3 geo FRs.
6. **Operations hardening** ([10](specs/10-deployment-and-operations.md)): backup & restore story (Q13) with the restore drill wired release-gating; migration execution policy (Q20); operation-table hygiene (Q33); upgrade-path drill live from the previous tags.

**Read first**
- **Features:** [F-012](features/F-012-live-updates.md) · [F-013](features/F-013-duplicate-detection.md) · [F-016](features/F-016-archive-download.md) · [F-009](features/F-009-reprocessing.md) · [F-017](features/F-017-views.md) (map part)
- **Specs:** [08-api-principles](specs/08-api-principles.md) · [12-reliability](specs/12-reliability.md) (doorbells, queue hygiene, client-visible idempotency) · [04-ingestion-pipeline](specs/04-ingestion-pipeline.md) (reprocessing, status API) · [06-search](specs/06-search.md) (map aggregation) · [09-previews](specs/09-previews.md) (artifact storage) · [10-deployment-and-operations](specs/10-deployment-and-operations.md)
- **ADRs:** [ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md) · [ADR-0007](decisions/ADR-0007-unified-event-log.md)

**Answer at entry:**
- **Q18** — WebSocket authentication (leaning: one-time ticket)
- **Q17** — scheduling configuration surface (caps + pause in v1; time windows?)
- **Q33** — operation-table hygiene & history retention
- **Q13** — backup & restore (must be resolved before v1 ships; drill or it isn't a backup)
- **Q20** — migration execution on upgrade
- **Q35** — map tile sourcing (zero-external-calls default vs. opt-in provider)

**Exit criteria:**
- Tag edit / extraction completion / permission revocation each reach every affected client over WS within the coalescing rules; a revoked user stops receiving events for that subtree; offline client reconciles via `/events` cursor.
- Archive of a large folder builds async with progress, resumes via Range, and re-validates permissions on every request; identical visibility shares one cached artifact.
- Selective reprocess replaces `auto` results, preserves `manual`/`confirmed`/`rejected`, and rolls back a bad generation.
- Restore drill green: backup → restore into a fresh stack → smoke suite. Upgrade drill green: previous tagged release → upgrade → smoke.
- **Web v1:** every P0/P1 feature tagged `Clients: all` computes `Implemented` for its FRs; benchmark thresholds hold; phase tagged as the web-v1 release.

---

## Phase 6 — Mobile foundation

**New segment:** the cloud in your pocket, read path first — native Android/iOS apps that pair by QR, browse and search the full library at 100k-item scale, open files at positions, and stay useful offline through the client cache.

**Build order:**

1. **Client decisions** — Q46 (native vs. reused-web per surface), Q40 (TLS trust policy), Q45 (distribution channels).
2. **[F-019](features/F-019-mobile-connection.md) — connection & device sessions**: URL connect, QR pairing (web shows the code — a web-UI capability), per-device PATs listed/revocable server-side, offline-tolerant failure behavior.
3. **[F-020](features/F-020-mobile-library.md) — library, timeline & viewer**: histogram-driven scroll geometry, scrubber, placeholder-first grid, immutability-exploiting thumbnail cache, per-class viewer opening at positions; navigation mirrors the server-side view set.
4. **[F-026](features/F-026-offline-cache-and-prefetch.md) — offline cache & prefetch** *(Clients: all — web gets the same semantics, best-effort under browser eviction)*: stale-while-revalidate rendering contract, doorbell + `/events` invalidation, `401/403/404/410` backstop, lock-then-wipe auth policy, prefetch (visible views, next pages, viewport subfolders, lightbox neighbors), budgets/GC (Q48/Q49).

**Read first**
- **Features:** [F-019](features/F-019-mobile-connection.md) · [F-020](features/F-020-mobile-library.md) · [F-026](features/F-026-offline-cache-and-prefetch.md)
- **Specs:** [13-mobile-clients](specs/13-mobile-clients.md) · [14-client-sync-and-caching](specs/14-client-sync-and-caching.md) · [07-identity-permissions-sharing](specs/07-identity-permissions-sharing.md) (tokens) · [09-previews](specs/09-previews.md) (thumbnails, preview descriptor) · [06-search](specs/06-search.md) (histogram/projection)
- **ADRs:** [ADR-0009](decisions/ADR-0009-external-traefik-edge.md) (TLS context for Q40)

**Answer at entry:**
- **Q40** — mobile TLS trust policy (CA-only vs. TOFU pinning for self-signed)
- **Q46** — mobile UI implementation strategy (couples to Q26)
- **Q45** — distribution channels & store review (permission strategy per build; carries into phase 7)
- **Q48** — client cache retention defaults
- **Q49** — client cache budget structure (explicitly blocks F-026 implementation)

**Exit criteria:**
- Pair a phone by QR against a real instance; revoke the device server-side → app locks cached content (lock-then-wipe policy per [14](specs/14-client-sync-and-caching.md)).
- Timeline meets [F-020](features/F-020-mobile-library.md)'s performance bars at 100k items: complete scroll geometry from one histogram request, no blank cells while scrolling.
- Viewer opens video at timestamp and PDF at page from a search hit.
- Airplane mode: previously visited navigation, listings, cards, and thumbnails render read-only with staleness indication; reconnection reconciles via `/events`; revoked data is purged from cache.
- F-019, F-020, F-026 matrix green (per-platform FRs on both platforms); beta builds distributed via the Q45 channels; phase tagged.

---

## Phase 7 — Mobile backup & parity → v1.0

**New segment:** the phone backs itself up and the apps reach full parity — auto-upload with an exhaustively accounted ledger, safe storage reclaim, managed offline files, and every web surface (member *and* admin) reachable natively. **Exit = v1.0.**

**Build order:**

1. **[F-021](features/F-021-mobile-auto-upload.md) — auto-upload**: source discovery (MediaStore/SAF, PhotoKit/Files), backfill + continuous backup, byte-identical originals, durable ledger with *verified* = server-confirmed content hash, `hash-check` endpoint, platform background-execution per [13 § background matrix](specs/13-mobile-clients.md); the Q38 protocol choice pays off in the iOS background-upload extension.
2. **[F-022](features/F-022-device-storage-reclaim.md) — storage reclaim**: the five gates of [13 § reclaim gates](specs/13-mobile-clients.md#reclaim-gates) — verified, re-confirmed server-side at action time, locally unchanged, past age policy, OS-dialog + OS-trash only.
3. **[F-024](features/F-024-offline-files-and-downloads.md) — offline files & downloads**: one-off downloads + pinned items kept current, per-file state everywhere, the one-way principle — no server event ever deletes a local copy; honest badging + actions on trash/purge/revocation.
4. **[F-025](features/F-025-client-parity.md) — parity ratchet**: every `Clients: all` feature reachable on both apps, admin surfaces included; parity gaps are spec bugs.

**Read first**
- **Features:** [F-021](features/F-021-mobile-auto-upload.md) · [F-022](features/F-022-device-storage-reclaim.md) · [F-024](features/F-024-offline-files-and-downloads.md) · [F-025](features/F-025-client-parity.md)
- **Specs:** [13-mobile-clients](specs/13-mobile-clients.md) (ledger, reclaim gates, asset groups, background matrix — normative) · [12-reliability](specs/12-reliability.md) (client-visible idempotency) · [03-storage-and-portability](specs/03-storage-and-portability.md) (uploads) · [08-api-principles](specs/08-api-principles.md) (parity = API-first)
- **ADRs:** —

**Answer at entry:**
- **Q44** — client-supplied capture-time hint (additive upload metadata; interaction with extraction)

**Exit criteria:**
- Fresh device with existing library: backfill completes with every in-scope item in exactly one accounted ledger state; new capture and edited item arrive (edit as new version); app killed mid-upload resumes without duplicate content ([12 § client-visible idempotency](specs/12-reliability.md)).
- Reclaim offer only for gate-passing items; server re-confirmation at action time; deletion only via the platform dialog into platform trash — negative test: an unverified or locally-modified item is never offered.
- Pinned file updates when the server version changes; server-side trash/purge/revocation leaves the local copy present and badged with restore/export/re-upload actions.
- [F-025/FR-1](features/F-025-client-parity.md) parity bar green across both platforms, admin surfaces included.
- **v1.0:** every P0/P1 feature computes `Implemented`; all drills and benchmark gates green; v1.0 tagged.

---

## Post-v1 pool

Explicitly later. Pulling an item forward = maintenance rule 6 (assign a phase; write the feature file first if none exists). Ordering within the pool is decided when pulling, not now.

| Item | What | Reading / gates when pulled |
|---|---|---|
| [F-018](features/F-018-people.md) — People | Faces, persons, naming, account links (fully specified, additive) | [ADR-0011](decisions/ADR-0011-person-recognition-architecture.md) · [ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md) · Q50, Q51, Q52, Q53 |
| [F-010](features/F-010-auto-sort-inbox.md) — Auto-sort inbox | Rule- then AI-driven sorting; its v1 obligations (move API, observable events, workspace-model flexibility) are already delivered in phases 1 and 5 | Q2 (inbox destination) |
| [F-023](features/F-023-os-file-manager-integration.md) — OS file-manager integration | iOS Files / Android DocumentsProvider surfaces; P2 — pull into a mobile phase if wanted sooner | [13-mobile-clients](specs/13-mobile-clients.md) |
| External workspace sources | GDrive & co., read-only mirrored ([03 § workspace sources](specs/03-storage-and-portability.md#workspace-sources)) | Q16 |
| Saved-search subscriptions | Standing queries notifying on new matches | Q37 (likely needs Q41) |
| Push notifications | Vendor-relay story for self-hosted (APNs/FCM/UnifiedPush) | Q41 |
| Mobile multi-account / multi-server | Account switching in the apps | Q39 |
| Silent reclaim (`MANAGE_MEDIA`) | Dialog-free auto-clean on Android | Q43 |
| iOS backup wake boosters | Geofence/iBeacon wake events | Q47 |
| Remote-AI extractors | Network-enabled extractors + credential handling | Q19 ([05 § hardening](specs/05-extractor-contract.md#container-requirements-hardening)) |
| Static collections/albums | Hand-picked ordered groupings — feature or explicit non-goal | Q36 |
| Folder-tag inheritance | Query-time tag inheritance to contents | Q23 |
| Folder-identity review queue | Human confirmation for ambiguous re-scan matches | Q24 |
| Mutation testing | Test-quality gate on the authz core | Q29 |
| Desktop sync / WebDAV / S3 access | [00 § deferred](specs/00-vision-and-goals.md#deferred-explicitly-wanted-later-api-must-not-preclude) | needs feature file(s) |
| Local AI agent | An API consumer ([00 § deferred](specs/00-vision-and-goals.md#deferred-explicitly-wanted-later-api-must-not-preclude)) | needs feature file |
| CLI client | Listed as an API consumer in [01](specs/01-architecture.md); no feature file yet | needs feature file |

## Cross-phase tracks

Standing work that grows every phase rather than belonging to one:

- **Corpus & benchmark** — the ground-truth corpus (Q28) gains fixtures with every capability (documents in 2, images/AV in 3, adversarial set with 1); the golden-query benchmark (Q8) guards ranking from phase 3 on.
- **Deployment stays installable** — the Compose stack deploys cleanly at every phase exit; every exit is tagged, feeding the upgrade-path drill; backup/restore docs harden through phases 4–5.
- **Security** — sandbox enforcement verified from phase 2 (Q7); image/secret/dependency scans from phase 0; permission/leak suites grow with phases 3–4; Q19 joins when remote extractors do.
- **Documentation** — operator docs (install, backup, upgrade) and third-party extractor-author docs (contract + conformance kit) grow with the phases that create their subjects.
