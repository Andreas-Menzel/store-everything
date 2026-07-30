# F-012 — Live Updates (WebSocket)

**Status:** Draft
**Priority:** P1
**Depends on:** F-011 (shared event log)
**Related specs:** [08-api-principles](../specs/08-api-principles.md), [ADR-0007](../decisions/ADR-0007-unified-event-log.md)

## Summary

The UI is fully live-updating: when a file changes, tags are edited, extraction completes, or permissions are revoked, every affected client is notified over WebSocket — ideally for everything. Notifications are **thin** (resource + event kind, no payload; the client refetches via the normal API), **permission-routed** (you only hear about what you can read), and **coalesced** (30 tag writes → one notification). Events originate from the database transaction itself via the outbox (ADR-0007), so the socket can never disagree with the data.

## User stories

- As a user with the app open in two windows, I want a tag edit in one to appear in the other within seconds.
- As a user watching a folder, I want files to appear as another user (or an import) adds them, and extraction status to tick over live.
- As an admin revoking someone's access, I want their client to drop the affected files immediately.

## Functional requirements

- **FR-1** Authenticated WebSocket endpoint; a connection receives notifications only for resources its user can read.
- **FR-2** Notifications are thin: `{resource_type, resource_id, event_kind, cursor}` — no payloads. Clients refetch through the normal API (API-first stays honest; no second payload format).
- **FR-3** Coalescing per (resource, event kind) within a short window (~1–2 s): a 30-tag bulk action produces one `file.tags_changed` notification.
- **FR-4** Permission revocation pushes an event to the affected user's connections; clients drop/refresh the resource. Grants likewise announce newly visible resources.
- **FR-5** Reconnect/catch-up via the `/events` cursor feed — a client offline during N changes resumes exactly; the socket is an optimization of the feed, not the source of truth.
- **FR-6** Coverage: file created/changed/moved/deleted, new versions, tag/metadata edits, extraction status transitions, permission and share changes, job progress (throttled).
- **FR-7** Events are emitted transactionally with the change (outbox); `LISTEN/NOTIFY` wakes the dispatcher.

## API surface

`WS /api/v1/ws` · `GET /events` (cursor catch-up)

## Out of scope

Payload deltas over the socket; per-field subscriptions; webhooks to external systems (later).

## Open questions

Whether an SSE fallback is worth offering for constrained clients — decide during API detail design.

## Acceptance criteria

- Client B sees client A's tag edit in < 2 s; a 30-tag bulk arrives as one notification.
- A revoked user's open client is notified and the file disappears from their view without a manual refresh.
- A client offline through 100 changes catches up completely and exactly via the cursor feed on reconnect.
- No notification is ever delivered for a resource the receiving user cannot read.
