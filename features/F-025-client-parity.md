# F-025 — Native App Parity (Full Web Feature Set)

**Status:** Draft
**Priority:** P1
**Clients:** Android, iOS — this feature *is* the parity rule against the web baseline
**Depends on:** [F-019](F-019-mobile-connection.md); references every feature tagged `Clients: all`
**Related specs:** [08-api-principles](../specs/08-api-principles.md#api-first-concretely), [13-mobile-clients](../specs/13-mobile-clients.md)

## Summary

The native apps expose **everything the web app exposes — member and admin surfaces alike**. There is no "web-only" capability tier: a feature reachable in the web UI and missing on mobile is a spec bug. Whether a given surface is implemented as native UI or as reused web UI is deliberately open ([Q46](../OPEN-QUESTIONS.md), coupled to the [Q26](../OPEN-QUESTIONS.md) frontend-stack decision) — the FRs below are implementation-neutral so either strategy satisfies them. Everything runs through the same API ([08](../specs/08-api-principles.md#api-first-concretely)); parity is therefore a UI-reachability obligation, never new server behavior.

## User stories

- As a user, I want the full search feature set — filters, facets, map, positional hits — on my phone so that the product's core promise doesn't require a desk.
- As an admin away from a computer, I want to approve a suggested tag, check extractor queues, or trigger a reprocess from my phone so that administration isn't gated on hardware.
- As a user, I want to share a file from any app on my phone straight into a cloud folder so that the phone's own workflows feed the cloud.

## Functional requirements

- **FR-1** **The parity rule:** every feature whose index entry is tagged `Clients: all` has all of its user-facing operations reachable in the native apps. The per-feature obligations of FR-2–FR-12 enumerate this for the current feature set; a future `Clients: all` feature joins this rule at creation.
- **FR-2** Search ([F-002](F-002-hybrid-search.md)): full request surface — query text, modes, all filters (class, type, tags with hierarchy/provenance options, metadata ranges, dates, size, workspace/folder scope, geo), facets with pending counts, `why` explainability, version and lifecycle scopes, and the [F-017](F-017-views.md) navigation contract: anchored hits open the viewer at position ([F-020/FR-11–12](F-020-mobile-library.md)), folder hits open browse.
- **FR-3** Views ([F-017](F-017-views.md)): execute, create from the current search, edit, delete, hide/reorder — personal and (for admins) system views.
- **FR-4** Map: the map layout executes geo grid aggregation ([F-002/FR-18](F-002-hybrid-search.md)) with pan/zoom-composed bounding boxes and "search this area"; tile sourcing follows the [Q35](../OPEN-QUESTIONS.md) decision on all clients equally.
- **FR-5** Files & folders ([F-001](F-001-upload-and-import.md), [F-007](F-007-versioning.md), [F-015](F-015-folders.md)): manual upload into a chosen folder (including camera capture on device), new-version upload, create/rename/move (incl. cross-workspace), version list and restore, move/rename of files, folder aggregates.
- **FR-6** Tags & metadata ([F-003](F-003-tagging.md)): apply/remove, confirm/reject with provenance display, metadata view/edit, tag autocomplete with the same semantics.
- **FR-7** Deletion lifecycle ([F-014](F-014-deletion-and-trash.md)): delete-to-trash, trash listing with badges/deadlines, restore (single and batch), purge with its documented friction.
- **FR-8** Duplicates ([F-013](F-013-duplicate-detection.md)): the groups view with scope filter and bulk resolution.
- **FR-9** Sharing ([F-008](F-008-sharing-and-public-links.md)): create/list/revoke grants and share links incl. expiry/password; archives ([F-016](F-016-archive-download.md)): request, progress, resumable download.
- **FR-10** Activity & stats: per-file activity ([F-011](F-011-audit-trail.md)), own storage stats ([09 § disk-usage](../specs/09-previews.md#disk-usage-visibility)).
- **FR-11** **Admin surfaces**, for admin accounts: user management, taxonomy governance incl. suggestion approval ([F-003/FR-12](F-003-tagging.md)), extractor registry/queues/health ([04 § status](../specs/04-ingestion-pipeline.md#status--observability-api-visible)), reprocessing trigger/pause/rollback ([F-009](F-009-reprocessing.md)), instance audit ([F-011](F-011-audit-trail.md)), instance storage stats, and the admin trash operations with their typed confirmations ([F-014/FR-8](F-014-deletion-and-trash.md)).
- **FR-12** Live behavior ([F-012](F-012-live-updates.md)): open screens refresh on notifications with cursor catch-up on reconnect — the same contract as the web client.
- **FR-13** **OS share target** *(Android, iOS — native-only: the OS share sheet is the platform capability)*: the app registers as a share target; content shared from any app uploads into a user-chosen destination via the normal upload path, honoring [F-001/FR-7](F-001-upload-and-import.md) collision handling.
- **FR-14** *(negative space)* The native apps hold **no privileged side channel**: every operation they perform is an ordinary documented API call a web client could equally make ([08 § API-first rule 1](../specs/08-api-principles.md#api-first-concretely)) — verified by tracing app operation coverage against the OpenAPI surface.

## API surface

Adds none — this feature is a client-coverage obligation over the existing surface.

## Out of scope

Which surfaces are native UI vs. reused web UI ([Q46](../OPEN-QUESTIONS.md)). Mobile-first *extensions* beyond the web set (they live in F-019–F-024). Feature-quality differences justified by form factor (e.g. keyboard-shortcut equivalents) — parity is about capability reachability, not identical widgets.

## Open questions

[Q46 (mobile UI implementation strategy — native / hybrid / reused web)](../OPEN-QUESTIONS.md), coupled to [Q26](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-2) The web's worked search examples ([F-002](F-002-hybrid-search.md) ACs: phrase-with-pages, video at 04:12, dog-at-the-beach, geo+class+tag) each succeed on the app with identical result sets (same API, asserted by response comparison) and open at their anchors.
- **AC-2** (FR-11) On the app, an admin approves a `suggested` tag, pauses an extractor queue, and triggers + rolls back a scoped reprocess; a member sees none of these surfaces.
- **AC-3** (FR-9) Creating a password-protected, expiring share link on the app produces a link that behaves per [F-008](F-008-sharing-and-public-links.md) ACs; revoking it on the web reflects in the app's list.
- **AC-4** (FR-13) Sharing a PDF from a third-party app into a chosen folder stores it byte-identically ([F-001](F-001-upload-and-import.md) rules) and it appears in listings with extraction pending.
- **AC-5** (FR-1) A parity checklist derived from the feature index (every `Clients: all` feature × its user-facing operations) executes on both platforms with zero unreachable entries — this checklist is the feature's standing regression test.
- **AC-6** (FR-14) The app's recorded API traffic over the full checklist run contains only documented `/api/v1` endpoints.
