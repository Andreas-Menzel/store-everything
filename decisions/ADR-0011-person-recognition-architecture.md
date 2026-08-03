# ADR-0011 — Person recognition: opt-in face extractor, core-owned per-owner identity resolution

**Status:** Accepted
**Date:** 2026-08-03

## Context

[F-005](../features/F-005-image-analysis.md) deliberately excluded face recognition with a recorded promise: privacy-sensitive, only ever as an explicit opt-in. Specifying the feature ([F-018](../features/F-018-people.md)) forces four decisions:

1. **Where inference lives.** Face detection/embedding is model inference; clustering faces into persons is stateful, incremental, cross-file work. The extractor contract ([ADR-0002](ADR-0002-extractor-containers-fixed-api.md), [05](../specs/05-extractor-contract.md)) is stateless per file version — clustering cannot be an extractor's job.
2. **Whose persons.** Face embeddings identify a human *across files*. Any scope wider than one user's data means the system matches biometric identity across permission boundaries.
3. **How user curation survives model upgrades.** A new face model detects *different* boxes; naming pinned to detection rows would be destroyed by every reprocess — the exact failure [ADR-0004](ADR-0004-tag-provenance-and-reprocessing.md) exists to prevent for tags.
4. **What "opt-in" means concretely** in a multi-user instance deployable on a public server, where biometric data is special-category (GDPR Art. 9-class) and the operator is accountable.

## Decision

- **Detection is an extractor; identity is core.** An official `face-detect` container emits per-face bounding boxes, quality scores, crops, and embeddings in a dedicated **`face-v1`** space (computed from the face region only), via one additive result kind `faces` — legal within `extractor-api/v1.x`. **Identity resolution** — incremental clustering of instances into persons, honoring user corrections — runs in the core as ordinary durable jobs ([ADR-0010](ADR-0010-crash-only-execution-model.md)), like duplicate grouping runs in core over extracted hashes. `face-v1` is **matching-only**: never cross-compared with other spaces, never a target of query-text embedding.
- **Persons are scoped per workspace owner.** Instances from a user's workspaces cluster into persons owned by that user; identity resolution never crosses owners. Rejected alternatives:
  - *Instance-global persons*: Alice naming a face would let the system identify that person inside Bob's private library — cross-user biometric matching is a leak by construction.
  - *Per-workspace persons*: identity fragments along storage layout (the same human named once per workspace), and ordinary same-owner moves would orphan confirmed appearances — colliding with move-invariant metadata ([F-010/FR-1](../features/F-010-auto-sort-inbox.md)) and "manual work survives" (ADR-0004).
  - *Persons as tags*: the tag vocabulary is a global, admin-governed DAG ([ADR-0006](ADR-0006-hierarchical-tags-dag.md)) — a shared curated vocabulary, the opposite governance and visibility model from owner-scoped PII.
  The only cross-owner join is an **explicit account link** (owner links a person to an instance account; the linked user always sees links to their account and can remove them).
- **Two-level data model, ADR-0004 verbatim.** `FaceInstance` (per file version: box, quality, embedding, crop; generation-scoped, atomically swapped by reprocessing) is machine evidence. `PersonAppearance` (per file × person: `manual | auto | confirmed | rejected` + confidence + source stamp) is the assertion layer: `rejected` suppresses re-adding forever; `manual`/`confirmed` survive reprocessing, re-anchored to the new generation's best IoU-matching instance and kept anchor-less when nothing matches. Invariant: an appearance's person is owned by the file's *current* workspace owner.
- **Opt-in is two-level and erasure is first-class.** Instance setting `disabled | default_off | default_on` (fresh installs: `default_off`, admin-set) plus per-workspace `inherit | on | off` (owner-set). Without effective enablement, no face data is created **or exposed**. Disabling *suspends* (data retained, hidden, volume reported — a config toggle must not destroy weeks of CPU work); deletion is an explicit owner-triggered **purge**. Admins configure defaults but get no person-data access ([07](../specs/07-identity-permissions-sharing.md)).
- **Visibility derives from file readability.** A caller observes a person iff they own it or can read ≥ 1 live, effectively-enabled file carrying its appearance; everything else is `404`-indistinguishable. No new grant machinery.

## Consequences

- Naming survives model upgrades; every confirm enriches the person's exemplar set (matching improves through curation, not model training on user data); every reject is permanent — the ADR-0004 guarantees extend to biometrics unchanged.
- Leak-safety needs no new permission system — person visibility, facets, and counts ride the existing in-query permission/lifecycle predicates, and are tested at the same rigor ([F-018/FR-26](../features/F-018-people.md)).
- The feature is purely additive (new result kind, new entities, new optional filter, new endpoints): v1 owes it nothing except the additivity that [05](../specs/05-extractor-contract.md) and [08](../specs/08-api-principles.md) already guarantee.
- Costs we accept: the same human is independent persons for different owners (deliberate — account links bridge it); core owns clustering quality and its tuning ([Q51](../OPEN-QUESTIONS.md)); suspend-vs-purge means disabled workspaces retain biometric data until an explicit purge (reported, never silent); face-recognition model licensing is a real redistribution risk to clear before shipping a default image ([Q50](../OPEN-QUESTIONS.md)).
