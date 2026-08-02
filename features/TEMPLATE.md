# F-NNN — Feature Title

**Status:** Draft | Review | Approved | Implemented | Deferred
**Priority:** P0 | P1 | P2
**Clients:** all · or a subset of web, Android, iOS — with a one-line reason when not `all` ([rule 9](README.md#writing-frs))
**Depends on:** F-XXX, …
**Related specs:** …

## Summary

One paragraph: what the feature does and why it exists.

## User stories

- As a …, I want … so that …

## Functional requirements

Numbered, testable, referenceable as `F-NNN/FR-n` — authoring rules in [Writing FRs](README.md#writing-frs). Ids are append-only (removed FRs stay as tombstones); FRs a plain deterministic test cannot verify declare their method inline.

- **FR-1** …
- **FR-2** *(verify: benchmark)* …
- **FR-3** *(removed — see ADR-00xx)*

## API surface

Endpoints/operations this feature adds or uses (sketch level while in Draft).

## Out of scope

What this feature explicitly does *not* cover.

## Open questions

Local questions, or links into [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md).

## Acceptance criteria

Numbered worked examples (`AC-n`) with concrete inputs and outputs, each naming the FR(s) it demonstrates — together the script for this feature's integration/E2E tests. Every FR must be verified by its declared method with its id in the test marker, enforced by the [traceability matrix](../specs/11-engineering-standards.md#requirement-traceability-the-matrix); the general [Definition of Done](../specs/11-engineering-standards.md#definition-of-done) applies on top of the criteria listed here.

- **AC-1** (FR-1, FR-2) …
