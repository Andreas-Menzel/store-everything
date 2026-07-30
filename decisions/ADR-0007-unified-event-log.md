# ADR-0007 — Unified event log via transactional outbox

**Status:** Accepted
**Date:** 2026-07-30

## Context

Three requirements turn out to be one mechanism: a **full audit trail** ("when was a file uploaded/updated/deleted, who did it — everything"), a **change feed** for sync clients and agents (`/events`), and a **fully live-updating UI** over WebSockets. Events must be complete (a change without an event is a bug), attributable (actor and intent, not just rows), and consistent with the data (no events for rolled-back transactions, no missed commits).

Raw database triggers were considered: they capture everything but see *rows*, not *intents* — they can't record the acting user, and one user action ("add 30 tags") looks like 30 anonymous row events. Debouncing is needed for the UI but must never degrade the audit record.

## Decision

- Application code writes semantic events (actor: user/extractor/system, action, resource, structured details) into an **append-only event log in the same database transaction as the change** (transactional outbox). PostgreSQL `LISTEN/NOTIFY` wakes the dispatcher — a doorbell, not a payload channel.
- Three consumers, three fidelities:
  - **Audit API** ([F-011](../features/F-011-audit-trail.md)): full fidelity, immutable, no coalescing — bulk actions record every item.
  - **`/events` cursor feed**: the full ordered sequence, for sync clients and agents that must not miss anything.
  - **WebSocket fan-out** ([F-012](../features/F-012-live-updates.md)): **thin** notifications (resource type, id, event kind — no payload; clients refetch via the normal API), **permission-routed** (a connection only hears about resources its user can read), and **coalesced** per (resource, event kind) within ~1–2 s — 30 tag writes become one `file.tags_changed`.
- Debouncing/coalescing exists **only** in the WebSocket layer, never in the log.

## Consequences

- One mechanism powers audit, sync, and live UI; the thin-notification pattern (à la Nextcloud `notify_push`) keeps API-first honest — no second payload format to maintain.
- Discipline required: every mutation must flow through code paths that emit events; out-of-band SQL would change state silently (mitigated by review + tests asserting event emission).
- The log grows forever by default → retention policies needed (audit retention configured separately from feed compaction).
- WebSocket routing needs an efficient user→readable-resources check; permission *revocation* itself is an event delivered to the revoked user.
