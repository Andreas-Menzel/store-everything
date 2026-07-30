# 11 — Engineering Standards

**Status:** Draft

Specs 00–10 define *what the system does*; this document defines *how changes are made*: workflow, testing, Definition of Done, commits, versioning, releases. It applies to every code repository of the project — and to this spec repo where meaningful.

## Docs first — read before, update after

1. **Before any change, read the governing documents**: the feature file (`features/F-NNN-…`), the affected `specs/`, related ADRs, and [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md). No implementation against documents you haven't read.
2. **Features are specified before they are implemented.** Every feature exists as a feature file — summary, numbered FRs, acceptance criteria — *before* code is written. No feature file, no implementation.
3. **After any change, update every affected document in the same change** — feature files, specs, OpenAPI schema, README map, user-facing docs. When a feature changes, its feature file changes with it. Stale docs are worse than none.
4. **Divergence is never silent.** If the implementation must deviate from a spec, the same change updates the spec — or records the open point in OPEN-QUESTIONS / a new ADR. Decisions are written down where they happen.

## Development workflow (the mandatory loop)

For every task, in order — steps are not batched and not skipped:

1. Read the governing docs (above); restate the acceptance criteria you are working toward.
2. Split the task into small, individually verifiable steps.
3. **Per step:** implement → self-review the diff → **update the test suite** (add / change / delete tests) → **run the affected tests**. Red stops the line — fix before starting the next step.
4. Before opening the MR: full test suite, linter, and type check green locally.
5. Update the docs (rule above) in the same change; write Conventional Commits.
6. Merge only when the [Definition of Done](#definition-of-done) holds: CI green, review threads resolved, checklist honestly ticked (or explicitly N/A).

## Testing

### Test layers (the pipeline)

| Layer | Scope | Backing services |
|---|---|---|
| **Unit** | pure logic: ranking fusion, permission resolution (union along the path), tag-DAG/closure operations, path/hash handling | none — fast, run constantly |
| **Integration** | API endpoints, queries, migrations, job queue | **real** PostgreSQL + pgvector in throwaway containers — no mocks-that-lie |
| **Contract** | extractor conformance kit: manifest, job lifecycle, result envelope — runnable against **any** extractor image, incl. third-party ones | the extractor image under test |
| **E2E** | compose up → upload → extract (stub extractor for speed) → search → share; web-UI flows | the full stack |

- **Headless by default.** Every test — including E2E/UI — runs unattended in CI. A check a human has to remember is not a test.
- **UI mode on demand.** The E2E/UI layer must also run headed, in a "UI mode" (watch the browser act, step through, inspect traces — e.g. Playwright's UI mode; tool choice open until the frontend stack is fixed). Headless and headed run the **same** tests — UI mode is a lens, never a separate suite.
- Tests are **isolated** (no order dependence) and fast enough that running them beats checking by hand.

### What must be tested

| Area | Requirement |
|---|---|
| Feature requirements | **Every FR maps to ≥ 1 automated test**, traceable via its id (`F-003/FR-2`) in the test name/marker. A feature file's acceptance criteria are the script for its integration/E2E tests. A feature is *done* only when its criteria run unattended in CI. |
| Permissions & auth | Exhaustive, incl. **negative cases**: every endpoint rejects unauthenticated calls; object-level checks (Bob cannot reach Alice's file by id); **permission-aware search from day one** ([07](07-identity-permissions-sharing.md#search-and-permissions)); share-link scope isolation ([F-008](../features/F-008-sharing-and-public-links.md)). |
| Deletion & trash | Same leak-test rigor as permissions: trashed items appear in **no** default surface — results, facets, counts, autocomplete, duplicate groups — including semantic-only queries ([02 § Invariants](02-domain-model.md#invariants) #7, [F-014](../features/F-014-deletion-and-trash.md)); purge leaves zero domain rows and honors version-blob refcounts; archive downloads re-validate permissions on **every** Range request ([F-016/FR-5](../features/F-016-archive-download.md)). |
| Domain invariants | Each invariant in [02 § Invariants](02-domain-model.md#invariants) has dedicated tests: originals never modified; provenance stamping; `manual`/`confirmed` survive reprocessing; `rejected` suppresses re-adding; event log written in the same transaction. |
| API contract | Responses validate against the OpenAPI schema; problem-details envelope and pagination envelope are shape-tested ([08](08-api-principles.md)). |
| Migrations | Every migration runs **up and down** in CI against a real database ([10](10-deployment-and-operations.md#upgrades--migrations)). |
| Search ranking | The golden-query benchmark (Q8) runs as a regression suite — ranking changes are measured, never guessed. |

### Coverage

- CI gate: **≥ 85 % line coverage** project-wide. The gate only ratchets up — coverage never decreases.
- Security-critical code (authz checks, permission-aware search, share links, token handling) is **not** satisfied by a number: exhaustive tests including negative cases, reviewed as such.
- Coverage is a floor, not a target — tests exist to catch regressions, not to color lines green.

## Definition of Done

A task is done when every line is honestly true — or explicitly marked N/A in the MR:

- [ ] Feature file exists / is updated; acceptance criteria & FRs demonstrably met — **by tests, not by hand**.
- [ ] Tests added/changed/removed to match the behaviour; full suite green in CI; coverage gate met.
- [ ] All affected docs updated **in this change** (feature file, specs, OpenAPI, README, user-facing docs, OPEN-QUESTIONS).
- [ ] API changes are additive within the major; schema regenerated; generated clients not stale ([08](08-api-principles.md)).
- [ ] Schema changes ship as a versioned migration, up **and** down tested, expand–contract safe ([10](10-deployment-and-operations.md#upgrades--migrations)).
- [ ] New/changed dependency has a recorded justification; lockfile committed.
- [ ] Conventional Commits; MR small and focused, linked to its issue; all threads resolved.

The feature template links this checklist; feature-specific criteria come **on top of** it, never instead of it.

## Git & commits

- Trunk is `main`, protected: no direct pushes, MR + green pipeline required. Work happens on short-lived `feature/*` / `fix/*` / `chore/*` branches (< ~2 days), deleted on merge. (Deliberately simpler than a two-trunk flow: a self-hosted product has tagged releases, not a staging trunk.)
- **Conventional Commits are required:** `type(scope): subject` with types `feat` `fix` `docs` `refactor` `perf` `test` `build` `ci` `chore` `revert`; breaking changes marked `feat!:` or a `BREAKING CHANGE:` footer; issues referenced in footers (`Refs: #123` / `Resolves: #123`).
- **Configured AND enforced:** commit format, lint, types, tests, coverage — every declared convention is a CI gate. An unenforced convention is a suggestion that rots.

## Versioning & releases

- **App releases follow SemVer**, derived from the Conventional Commits since the last tag: `fix:` → patch, `feat:` → minor, breaking → major.
- **One command creates a release:** `make release` — derives the bump from the commits, updates `CHANGELOG.md`, bumps the version, creates the annotated git tag. (Proposed tool: Commitizen `cz bump` — language-independent, so it doesn't prejudge Q10.) Manual tags and hand-edited changelogs are forbidden; **CI builds and publishes images from tags only**.
- **Version lines are independent.** App SemVer, API major ([08](08-api-principles.md), `/api/v1`), and extractor contract (`extractor-api/v1`, [05](05-extractor-contract.md)) deliberately do **not** imply one another. Compatibility is promised in exactly one place: a **support matrix** in the repo — restated per release in the release notes — recording which app versions serve which API major(s) and speak which extractor-contract version(s). Dropping an API/contract major follows the deprecation rules in [08](08-api-principles.md) and becomes visible in the matrix, never through a version-number convention.
- Database schema versions are internal: ordered migrations per release ([10](10-deployment-and-operations.md#upgrades--migrations)), never user-facing.

## Dependencies & configuration

- Pin and lock everything; commit the lockfile — reproducible builds, always.
- A new dependency (or a swap of an established one) needs a deliberate, recorded justification.
- 12-factor throughout: config from the environment; stateless processes (state lives in PostgreSQL); logs to stdout ([10](10-deployment-and-operations.md#logging)); graceful shutdown on SIGTERM — which for hours-long extraction jobs means re-queue, at-least-once delivery, idempotent result writes ([04](04-ingestion-pipeline.md), [05](05-extractor-contract.md#job-lifecycle)).

## CI pipeline (the enforcement list)

All blocking: lint + format check · type check · unit tests · integration tests (real PostgreSQL + pgvector) · migrations up/down · extractor conformance kit (official extractors) · E2E headless · coverage gate (≥ 85 %, ratcheting) · OpenAPI schema in sync + generated clients not stale · commit-format check · secret scan · dependency vulnerability scan · image scan (core + official extractor images) · SBOM generation.
