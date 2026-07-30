# F-010 — Auto-Sort Inbox Workspace

**Status:** Deferred (design sketch — v1 only guarantees the primitives)
**Priority:** P2
**Depends on:** F-001, F-002; move API ([08-api-principles](../specs/08-api-principles.md))
**Related specs:** [03-storage-and-portability](../specs/03-storage-and-portability.md#the-auto-sort-inbox-deferred-feature-keep-possible)

## Summary

A drop-zone workspace for uploads with no chosen destination: files land in an inbox and get organized automatically — initially by simple rules (year/month folders derived from file dates), later optionally by a local AI model proposing or executing moves into the right place. Because moves are first-class API operations, this whole feature is *just an API consumer* — which is the point of API-first.

## Design sketch (to be specified when activated)

- Rule stage: deterministic sorting (e.g. `{year}/{month}` from EXIF taken-at, else mtime). Runs after ingestion completes so extracted metadata is available.
- AI stage (optional, local model): propose destination (existing folder taxonomy) with confidence; below threshold → suggestion awaiting user approval instead of silent move.
- Every automatic move is audited and reversible (undo restores previous path; file identity, versions, and tags are move-invariant by construction).
- Whether sorted destinations live inside the inbox workspace or in other workspaces is undecided — [Q2](../OPEN-QUESTIONS.md).

## v1 obligations (so this stays buildable later)

- **FR-1** `move` is a first-class API operation preserving file identity, versions, tags, permissions, and share links.
- **FR-2** Ingestion-complete events are observable (change feed) so a sorter can react.
- **FR-3** Workspace model doesn't hard-code "user browses only their own tree" assumptions that a system-driven mover would violate.

## Open questions

[Q2 (workspace layout, inbox destination)](../OPEN-QUESTIONS.md).
