# F-022 — Device Storage Reclaim ("Free Up Space")

**Status:** Draft
**Priority:** P1
**Clients:** Android, iOS — native-only because browsers cannot delete device media; deletion exists only behind OS-mediated APIs
**Depends on:** [F-021](F-021-mobile-auto-upload.md) (verified ledger)
**Related specs:** [13-mobile-clients](../specs/13-mobile-clients.md#reclaim-gates) (gates — normative)

## Summary

Delete device copies of media that is **provably in the cloud** — on request, and as recurring one-tap offers ("1 240 items backed up ≥ 30 days ago · 4.2 GB reclaimable"). Eligibility is the five-gate rule of [13 § reclaim gates](../specs/13-mobile-clients.md#reclaim-gates): verified by content hash, re-confirmed server-side *at action time*, locally unchanged, past the age policy, and deleted only through the platform's user-confirmed dialog into the platform's trash. Fully silent deletion does not exist on either platform's standard permission model — the automation is the *offer*, and that is a feature: after reclaim the bytes still exist twice (server copy + 30-day OS trash). Nothing is ever lost.

## User stories

- As a user with a full phone, I want one tap to free gigabytes of already-backed-up photos so that I never manually cross-check what's safe to delete.
- As a user, I want a monthly reminder of what's reclaimable so that cleanup happens without me thinking about it.
- As a user, I want certainty that nothing not-yet-uploaded, edited-since-upload, or since-deleted-from-the-cloud can ever be removed from my phone.

## Functional requirements

- **FR-1** **Eligibility** is evaluated at action time as the conjunction of [13 § reclaim gates](../specs/13-mobile-clients.md#reclaim-gates): ledger `verified` including all asset-group components · fresh server re-confirmation (mapped file live and owned) · local size+mtime unchanged since hashing (else re-hash and re-verify first) · age since verification ≥ the configured minimum (default 30 days; 0 permitted) · not excluded by FR-2.
- **FR-2** **Exclusions**, each visible in the review step: favorites kept by default (toggle) · per-source opt-out · messenger-app media folders (e.g. WhatsApp) excluded by default with the stated reason — their in-app chat media breaks when the files are removed.
- **FR-3** **Manual flow:** summary (item count, reclaimable bytes) → review list with per-item opt-out → platform deletion. *(Android)* `createTrashRequest` batches of ≤ 2 000 (platform cap), one system dialog per batch, items land in the system trash (~30 days). *(iOS)* asset deletion in batches, one system dialog per batch, items land in Recently Deleted (30 days).
- **FR-4** **Scheduled offers:** off / weekly / monthly / storage-low (default: monthly). Each trigger produces one local notification stating count and bytes; tapping opens the FR-3 flow. The notification itself deletes nothing.
- **FR-5** *(negative space)* No deletion path exists that bypasses the OS-mediated user confirmation, and this feature issues **no server-mutating request at all** — it deletes device copies only, confirmed by an API trace containing only reads.
- **FR-6** *(negative space)* An item is never deleted when any gate fails at action time: unverified, failed re-confirmation, server counterpart trashed or absent, modified since verification, or excluded. Items failing between review and execution are skipped and reported — never force-deleted to honor the shown count.
- **FR-7** **iOS honesty:** the completion state says that space is fully freed only when *Recently Deleted* empties (30 days or manually in Photos — no API can do it), and if iCloud Photos is enabled, the flow requires an explicit acknowledgment that deletion propagates to iCloud before any dialog is shown.
- **FR-8** **Undo guidance:** *(Android)* completed reclaims offer restore-from-system-trash for the OS retention window (via the corresponding un-trash request, one system dialog). *(iOS)* the completion state links to Photos › Recently Deleted with the recovery deadline.
- **FR-9** Reclaimed items remain recorded in the ledger as `reclaimed` (they are not re-suggested, and their cloud files remain in all library views); the reclaim history (when, how many, which batch) is inspectable on device.

## API surface

Adds no endpoints. Consumes `POST /files/hash-check` and `GET /files/{id}` for re-confirmation ([F-021](F-021-mobile-auto-upload.md)).

## Out of scope

Deleting anything server-side ([F-014](F-014-deletion-and-trash.md) owns that). Reclaiming files not covered by the auto-upload ledger. Silent (dialog-free) deletion — Android's opt-in `MANAGE_MEDIA` path is deliberately deferred to [Q43](../OPEN-QUESTIONS.md).

## Open questions

[Q43 (Android `MANAGE_MEDIA` silent reclaim)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-1, FR-3) 3 500 verified month-old photos: Android shows the review, then two system dialogs (2 000 + 1 500); afterwards the items are in the system trash, the server is byte-for-byte unchanged, and the app's timeline still shows all items (cloud copies).
- **AC-2** (FR-6) An item whose server file was trashed after verification is absent from the eligible set; one edited since verification likewise (until re-verified). Force-constructing a stale `verified` flag and running reclaim deletes neither (negative test).
- **AC-3** (FR-5) A full reclaim session's API trace contains zero non-GET requests; killing the app mid-flow deletes nothing beyond batches already confirmed through the OS dialog.
- **AC-4** (FR-2) A WhatsApp-folder source is excluded by default with the stated reason; enabling it moves its items into the eligible set.
- **AC-5** (FR-7) With iCloud Photos enabled, the flow blocks until the propagation acknowledgment; with it disabled, no acknowledgment is requested.
- **AC-6** (FR-4) With monthly offers enabled and eligible items present, exactly one notification fires per period; tapping it opens the review; ignoring it deletes nothing.
- **AC-7** (FR-8) *(Android)* Restore-from-trash after a reclaim returns the files; re-running reclaim re-offers them (still verified).
