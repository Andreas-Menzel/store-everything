# F-011 — Full Audit Trail

**Status:** Draft
**Priority:** P1
**Clients:** all
**Depends on:** —
**Related specs:** [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md), [ADR-0007](../decisions/ADR-0007-unified-event-log.md)

## Summary

Every state-changing action in the system is recorded: when was a file uploaded, updated, moved, deleted — and who did it. Uploads, versions, tag and metadata edits, permission grants and revocations, share-link creation and accesses, logins, reprocessing runs, extractor executions. The trail is append-only, attributable (user, extractor, or system), and written in the same transaction as the change itself (ADR-0007), so nothing changes silently.

## User stories

- As an admin, I want to see who did what across the instance so that I can investigate anything from a lost file to a suspicious login.
- As a user, I want the full history of one of my files (uploads, edits, tag changes, who accessed the share link) so that I understand what happened to it.
- As a user whose file is shared with others, I want tag/metadata edits attributed so that I know who changed what.

## Functional requirements

- **FR-1** Every state-changing action produces an audit record: actor (user / extractor / system), action type, resource, timestamp, structured details.
- **FR-2** The trail is append-only and immutable via API — no update or delete endpoints; retention-based pruning is the only removal and is itself audited.
- **FR-3** **Full fidelity**: bulk actions record every item (a 30-tag change loses nothing). Coalescing exists only in the WebSocket layer ([F-012](F-012-live-updates.md)), never here.
- **FR-4** Records are written in the same transaction as the change (transactional outbox) — no missed or phantom entries.
- **FR-5** Admin query: instance-wide, filterable by actor, resource, action type, and time range; cursor-paginated.
- **FR-6** User view: activity for their own files/workspaces and files shared to them, scoped to what they can read.
- **FR-7** Share-link accesses are logged (timestamp, link token) and drive the access counter in [F-008](F-008-sharing-and-public-links.md).
- **FR-8** Retention policy is admin-configurable; default generous.
- **FR-9** Records are **self-contained**: `details` carries human-readable identity (file/folder name and path at action time), so history stays meaningful after the resource is purged — after purge, events are the *only* remaining trace ([F-014/FR-7](F-014-deletion-and-trash.md)).

## API surface

`GET /audit` (admin, filters + cursor) · `GET /files/{id}/activity` · `GET /workspaces/{ws}/activity`

## Out of scope

External SIEM export / webhooks (later). Cryptographic tamper-evidence (hash chaining) — possible later hardening.

## Open questions

Retention defaults — decide with usage data.

## Acceptance criteria

- Bob (write permission) edits tags on Alice's file: the record appears in the file's activity with Bob's user id.
- A 30-tag bulk action yields 30 retrievable items in the trail.
- No API call can modify or delete an existing audit record.
- A change whose transaction rolls back leaves no audit record; a committed change always leaves one.
- After a file is purged, its lifecycle events (created … trashed … purged) remain queryable and display the file's name and path as of each action.
