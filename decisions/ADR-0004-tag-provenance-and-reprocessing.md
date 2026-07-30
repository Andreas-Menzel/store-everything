# ADR-0004 — Tag provenance model and reprocessing rules

**Status:** Accepted
**Date:** 2026-07-29

## Context

Tags are assigned manually by users and automatically by local AI extractors. Auto tags are guesses: they carry confidence, can be wrong, and will be regenerated wholesale when better models arrive. User corrections must never be undone by a machine. Tags and metadata belong to the *file* (shared truth among permitted users), so provenance must also record *who/what* set each value.

## Decision

Every applied tag (`FileTag`) carries a **provenance** state and full source stamping (user id, or extractor id + version + model version + generation; confidence for auto tags):

| State | Set by | Reprocessing behavior |
|---|---|---|
| `manual` | user | untouchable |
| `auto` | extractor | replaced by the new generation's output |
| `confirmed` | user approving an `auto` tag | becomes user truth — untouchable, like `manual` |
| `rejected` | user removing an `auto` tag | kept as a negative record — suppresses re-adding by any future generation |

Reprocessing runs as **generations**: a new `ExtractionRun` generation replaces the previous generation's `auto` outputs (tags, metadata, segments, embeddings) atomically per file *after* completing; the prior generation is retained for rollback until explicitly pruned. The same provenance/generation rules apply to auto-derived metadata, not just tags.

Auto tags are visibly labeled as such (with confidence) in every API response.

## Consequences

- Users can trust their curation: no model update ever destroys manual work, and corrections stick (`rejected` prevents the "fox → cat comes back" failure).
- Confirm/reject is cheap curation UX and doubles as ground-truth data if we ever want to evaluate model quality per instance.
- Costs: tag storage is state-machine-like rather than a simple set; reprocessing must diff against `rejected`/`confirmed` records; rollback requires keeping one superseded generation (storage overhead, pruning policy needed).
