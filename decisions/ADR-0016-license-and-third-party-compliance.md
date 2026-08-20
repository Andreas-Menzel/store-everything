# ADR-0016 — AGPL-3.0, and third-party license compliance as a CI gate

**Status:** Accepted
**Date:** 2026-08-20

## Context

This repository will be published. That decision has to be made *before* the first line of code, because a repository that becomes public later inherits its entire history: a secret committed today is a secret leaked then, and a dependency or fixture adopted today without a license check is a compliance problem that has to be unwound later.

The project is a self-hosted product whose value proposition is that users run it themselves ([00](../specs/00-vision-and-goals.md)). The comparable products in this category — Immich, Nextcloud, Paperless-ngx — are AGPL-licensed, for the consistent reason that a copyleft-with-network-clause license keeps improvements flowing back while a hosted-service fork stays obligated to publish its changes.

The project also consumes third-party work in three distinct places, each with different obligations: runtime dependencies (Python and TypeScript trees), test fixtures ([ADR-0015](ADR-0015-ground-truth-corpus-strategy.md)), and model weights inside default extractor images — where "redistributable in our image" is a live constraint ([Q9](../OPEN-QUESTIONS.md), [Q50](../OPEN-QUESTIONS.md)), because several strong models carry research-only or non-commercial terms.

## Decision

**The project's own code is licensed under AGPL-3.0-only**, with the full license text at `LICENSE` and contributions accepted inbound-equals-outbound.

**The repository is treated as public from this point forward**, whether or not its visibility flag has been flipped yet. Concretely: no secrets in history at any time (enforced by the secret-scan gate), `.env` git-ignored with a dummy-valued `.env.example` committed ([10 § configuration](../specs/10-deployment-and-operations.md#configuration--secrets)), and every third-party artifact carrying a recorded license before it is committed.

**Third-party compliance is enforced, not documented.** Three mechanisms, all CI gates ([11 § CI](../specs/11-engineering-standards.md#ci-pipeline-the-enforcement-list)):

1. **Dependency license allow-list.** Both dependency trees are scanned; a dependency whose license is absent from the allow-list, or is incompatible with AGPL-3.0 distribution, fails the pipeline. Adding a license to the allow-list is a deliberate, reviewed change — the same bar as [11](../specs/11-engineering-standards.md#dependencies--configuration)'s recorded justification for the dependency itself.
2. **Generated third-party notice.** A `THIRD-PARTY-LICENSES` document is generated from the locked dependency trees at build time, shipped inside the image and attached to each release. It is generated, never hand-maintained, and a generation failure fails the build.
3. **Corpus attribution.** `corpus/ATTRIBUTION.md` is generated from the fixture manifest and committed, drift-checked in CI ([ADR-0015](ADR-0015-ground-truth-corpus-strategy.md)).

**Model weights in official extractor images inherit this bar.** A model whose license forbids redistribution or restricts commercial use cannot ship in a default image; it may at most be an opt-in image the operator pulls themselves. This is a hard constraint on [Q9](../OPEN-QUESTIONS.md) and [Q50](../OPEN-QUESTIONS.md), not a preference.

## Consequences

- **The network clause applies to us too**: anyone offering a modified Store Everything as a service must publish those modifications. That is the intended effect.
- **Some dependencies become unavailable** — proprietary or incompatibly-licensed libraries are rejected at the gate rather than discovered at publication time. The allow-list makes that trade explicit instead of implicit.
- **Attribution obligations are satisfied mechanically.** CC-BY fixtures and permissively-licensed dependencies are attributed by generated documents that cannot silently fall out of date.
- **Extractor authors are unaffected.** Third-party extractors are separate containers speaking a fixed API ([ADR-0002](ADR-0002-extractor-containers-fixed-api.md)); the contract is an interface, and this license governs our implementation, not theirs.
- **Publishing is now a flag flip, not a project.** The remaining decision — *when* to make the repository visible — is the owner's, and carries no cleanup work as long as these gates stay green from the first commit.
