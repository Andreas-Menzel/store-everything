# 11 — Engineering Standards

**Status:** Draft

Specs 00–10 define *what the system does*; this document defines *how changes are made*: workflow, code reuse & refactoring, testing, Definition of Done, commits, versioning, releases. It applies to every code repository of the project — and to this spec repo where meaningful.

## Docs first — read before, update after

1. **Before any change, read the governing documents**: the feature file (`features/F-NNN-…`), the affected `specs/`, related ADRs, and [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md). No implementation against documents you haven't read.
2. **Features are specified before they are implemented.** Every feature exists as a feature file — summary, numbered FRs, acceptance criteria — *before* code is written. No feature file, no implementation.
3. **After any change, update every affected document in the same change** — feature files, specs, OpenAPI schema, README map, user-facing docs. When a feature changes, its feature file changes with it. Stale docs are worse than none.
4. **Divergence is never silent.** If the implementation must deviate from a spec, the same change updates the spec — or records the open point in OPEN-QUESTIONS / a new ADR. Decisions are written down where they happen.

## Development workflow (the mandatory loop)

For every task, in order — steps are not batched and not skipped:

1. Read the governing docs (above); restate the acceptance criteria you are working toward.
2. Split the task into small, individually verifiable steps.
3. **Per step:** implement → self-review the diff (incl. the two standing [reuse questions](#where-to-look)) → **update the test suite** (add / change / delete tests) → **run the affected tests**. Red stops the line — fix before starting the next step.
4. Before opening the MR: full test suite, linter, and type check green locally.
5. Update the docs (rule above) in the same change; write Conventional Commits.
6. Merge only when the [Definition of Done](#definition-of-done) holds: CI green, review threads resolved, checklist honestly ticked (or explicitly N/A).

## Code reuse & shared modules

One concept, one implementation. UI is the canonical case — **one** confirm dialog, **one** bottom sheet, **one** button — but the rule covers all code. The rules are stack-agnostic on purpose; the concrete frontend stack and tooling are open (Q26).

### The rules

1. **One canonical shared layer per repo.** Shared UI primitives (dialogs/modals, bottom sheets, buttons, toasts, form fields) and shared logic (formatters, validators, error handling) live in exactly one discoverable place. Nothing shared lives inside a feature.
2. **Check before create.** Before writing any new UI element or utility: check the shared layer and its showcase. If something close exists, extend it with a variant — never copy it into a feature.
3. **Shared modules are domain-free.** No `DeleteFileModal` — a generic `ConfirmDialog` with a `destructive` variant, configured by data (title, message, labels, callbacks). A shared component that knows what a workspace or a tag is has stopped being shared; business logic stays in features.
4. **Reuse the mechanism, not just the markup.** One `confirm(…)`-style API owns open/close state; one dialog primitive decides modal vs. bottom sheet by viewport. Features never re-implement these mechanics.
5. **Design tokens under everything.** Spacing, colors, typography, radii, z-index come from one theme — no hardcoded values, including in one-off UI.
6. **Cross-cutting states are solved once.** Loading/empty/error/disabled states, focus trap, ESC-to-close, scroll lock, keyboard navigation, ARIA — built into the shared layer, never re-solved (or forgotten) per feature.
7. **Dependencies point one way.** Features import from the shared layer; the shared layer never imports from features.
8. **Not just UI.** File-size/date formatters, the problem-details handler and pagination envelope ([08](08-api-principles.md#errors-rfc-9457)), WebSocket reconnect ([F-012](../features/F-012-live-updates.md)) are shared modules — and the web UI calls the API **only** through the generated OpenAPI client ([08](08-api-principles.md)); hand-rolled fetches are forbidden.

### When to abstract — four tests

Abstraction is a judgment with tests, not a vibe. Applied the moment similarity is noticed:

| Test | Question | Abstract when |
|---|---|---|
| **Divergence** | If the copies drift apart later, is that a bug? | Yes — they share a *reason to change*, not just a shape |
| **Naming** | Is there an honest, domain-free name? | `ConfirmDialog`, `formatFileSize` — yes. A name that needs the feature in it means resemblance, not a concept |
| **Parameters** | In the unified API, are the variants data or control flow? | Variants are data (title, tone, callback). Boolean flags selecting internal branches mean two things stapled together |
| **Count** | How many real uses exist? | A second concrete use, with the tests above passing → extract now. One use → keep it local, except below |

**Abstract at first use** — no waiting — when the further uses aren't speculation:

- **The spec already guarantees them:** every API consumer handles the problem-details envelope; a file app renders file sizes on every screen.
- **The requirement is cross-cutting by nature:** focus traps, permission-aware rendering, error toasts come from global requirements and were never feature-local to begin with.

The counter-rule carries equal weight: **duplication is cheaper than the wrong abstraction.** No speculative extraction, no config-flag god-components. When in doubt, duplicate and revisit at the second use.

### Where to look

- **Before writing — the shared layer and its showcase.** The mandatory first stop, every time.
- **Before writing — the feature files.** Docs-first means the reuse landscape is readable before code exists: [F-014](../features/F-014-deletion-and-trash.md) alone specifies several destructive confirmations (delete, purge, empty trash — instance-wide even *typed*), bulk reprocessing ([F-009](../features/F-009-reprocessing.md)) adds another. Scanning `features/` answers "will this have a second use?" with facts, not guesses.
- **After writing — self-review** (workflow step 3) answers two standing questions: *Did I copy anything into existence? Did I rebuild something the shared layer already had?*
- **Review** is the human backstop: the [Definition of Done](#definition-of-done) includes the reuse check; CI adds an advisory duplication report.

### Enforcement

Per this document's own rule — configured AND enforced: lint forbids raw UI primitives (`<button>`, `<dialog>`, hardcoded colors/spacing) outside the shared layer and enforces the import direction; a showcase page (Storybook-class) is the living component inventory — and a target for the E2E UI layer; CI runs an advisory copy-paste detector. Concrete tools land with the frontend stack (Q26).

## Refactor over quick fix

Every change is made with the whole project in view. "It works now" is not a bar — done means *fits the system*.

1. **No quick fixes.** A change that fights the current structure — a special case where a concept belongs, a copy where a variant belongs, a workaround where a refactor belongs — is not done, even if green.
2. **If the clean change needs a refactor, the refactor is in scope.** Reshape first, then build on it — as separate commits (or a preparatory MR), so the refactor stays reviewable and the feature diff stays small. Never a silent drive-by.
3. **Extraction is part of the task that triggers it.** When the [four tests](#when-to-abstract--four-tests) say "shared concept", creating the shared module belongs to the current task; migrating the existing call sites may follow as its own small MR immediately after — not as a "later" that never comes.
4. **Known debt is recorded, never implicit.** If a shortcut is consciously taken anyway (deadline, blocked decision), it is written down — an issue or an [OPEN-QUESTIONS](../OPEN-QUESTIONS.md) row — naming what the right change would be. Divergence is never silent, in code as in docs.

## Testing

### Test layers (the pipeline)

| Layer | Scope | Backing services |
|---|---|---|
| **Unit** | pure logic: ranking fusion, permission resolution (union along the path), tag-DAG/closure operations, path/hash handling — example-based **and property-based**: algebraic invariants (closure expansion terminates on any DAG; a permission union never grants more than the union of its grants; pagination never skips or duplicates an item) are tested as properties, not just examples | none — fast, run constantly |
| **Integration** | API endpoints, queries, migrations, job queue | **real** PostgreSQL + pgvector in throwaway containers — no mocks-that-lie |
| **Contract** | extractor conformance kit: manifest, job lifecycle, result envelope — runnable against **any** extractor image, incl. third-party ones | the extractor image under test |
| **E2E** | compose up → upload → extract (stub extractor for speed) → search → share; web-UI flows | the full stack |
| **Fault-injection** | kill the process at fault points around every filesystem mutation and state transition, restart, assert convergence: terminal state reached, no debris past grace windows, no duplicated effects or events ([12](12-reliability.md#verification)) — the home of the `fault-injection` verification method | real PostgreSQL + scratch filesystem |

- **Headless by default.** Every test — including E2E/UI — runs unattended in CI. A check a human has to remember is not a test.
- **UI mode on demand.** The E2E/UI layer must also run headed, in a "UI mode" (watch the browser act, step through, inspect traces — e.g. Playwright's UI mode; tool choice open until the frontend stack is fixed — Q26). Headless and headed run the **same** tests — UI mode is a lens, never a separate suite.
- Tests are **isolated** (no order dependence) and fast enough that running them beats checking by hand.
- **A flaky test is red.** Quarantining requires an expiry date and a linked issue — visible, never silently retried into green.
- **Time is injected.** The core reads "now" only through an injectable clock — date facets, versioning, retention, and trash expiry are deterministic under test.

### Verification methods (per FR)

Every FR is verified by a **declared method**; the feature file marks any FR whose method isn't the default (`**FR-10** *(verify: benchmark)* …` — see [Writing FRs](../features/README.md#writing-frs)):

| Method | Meaning | Runs |
|---|---|---|
| `test` *(default, unmarked)* | Deterministic automated test. Default home: the **integration layer, through the public API** — API-first makes the API the falsification surface. Unit for pure logic; E2E stays a thin smoke path. | per MR, blocking |
| `benchmark` | Threshold-scored suite over the ground-truth corpus: quality metrics for statistical FRs (recall@k / NDCG — Q8), latency percentiles on reference hardware (Q27) for performance FRs (e.g. [F-002/FR-10](../features/F-002-hybrid-search.md)). Model versions pinned; trends tracked. | scheduled + pre-release; release-blocking |
| `fault-injection` | Crash-semantics tests: worker killed mid-job, duplicate result delivery, orchestrator restart — generalized by the crash-only harness to every effectful operation ([04](04-ingestion-pipeline.md), [05](05-extractor-contract.md#job-lifecycle), [12](12-reliability.md#verification)). | per MR, blocking |
| `drill` | Exercised procedure: restore — backup → restore into a fresh stack → smoke suite (Q13); upgrade-path — seed data on the previous tagged release → upgrade → smoke ([10](10-deployment-and-operations.md#upgrades--migrations)). | scheduled; release-blocking |

A test satisfies an FR only if it **fails when the FR is violated** — for "never / only / no" guarantees the negative case *is* the required test, not an extra.

### Requirement traceability (the matrix)

FR ids in test markers make requirement coverage checkable; the matrix makes it **checked**:

- Tests carry the ids they verify as structured markers (e.g. `@fr("F-002/FR-7")`; exact mechanism follows the stack, Q10). Domain invariants participate under stable ids (`02/INV-n` ↔ [02 § Invariants](02-domain-model.md#invariants) #n).
- CI regenerates the matrix on every run — one row per FR/invariant: id · requirement · feature + status · declared method · covering tests (name, layer) · last result · tombstone note. Published as a CI artifact with a summary on the MR; **not committed** (a committed generated file is a staleness bug waiting).
- **Hard gates (fail the pipeline):** a feature at `Implemented` — or moved there in the MR — with an FR lacking ≥ 1 passing verification of its declared method (`Implemented` is thereby a *computed* status, not a claim) · a test referencing an FR id that doesn't exist or is tombstoned.
- **Soft gates (warn):** an FR declaring a method whose suite isn't wired up yet · vague-word lint on FR lines ([Writing FRs](../features/README.md#writing-frs)).
- Tracing runs both ways: forward (FR → tests) proves coverage; backward (test → FR) catches dangling ids. **Not every test carries an FR marker** — bug regressions trace to issues, unit tests of internals to nothing; only FR-marked tests are cross-checked.
- Needed from the **first feature MR**; specified here language-agnostically (marker convention + script contract), implemented once Q10 resolves.

### What must be tested

| Area | Requirement |
|---|---|
| Feature requirements | **Every FR is verified by its declared method** ([above](#verification-methods-per-fr)), traceable via its id (`F-003/FR-2`) in the test marker, enforced by the [matrix](#requirement-traceability-the-matrix). A feature file's acceptance criteria (`AC-n`) are the script for its integration/E2E tests. A feature is *done* only when its criteria run unattended in CI. |
| Permissions & auth | Exhaustive, incl. **negative cases**: every endpoint rejects unauthenticated calls; object-level checks (Bob cannot reach Alice's file by id); **permission-aware search from day one** ([07](07-identity-permissions-sharing.md#search-and-permissions)); share-link scope isolation ([F-008](../features/F-008-sharing-and-public-links.md)). |
| Deletion & trash | Same leak-test rigor as permissions: trashed items appear in **no** default surface — results, facets, counts, autocomplete, duplicate groups — including semantic-only queries ([02 § Invariants](02-domain-model.md#invariants) #7, [F-014](../features/F-014-deletion-and-trash.md)); purge leaves zero domain rows and honors version-blob refcounts; archive downloads re-validate permissions on **every** Range request ([F-016/FR-5](../features/F-016-archive-download.md)). |
| Domain invariants | Each invariant in [02 § Invariants](02-domain-model.md#invariants) has dedicated tests: originals never modified; provenance stamping; `manual`/`confirmed` survive reprocessing; `rejected` suppresses re-adding; event log written in the same transaction. Invariants carry stable ids (`02/INV-n`) and appear in the traceability matrix like FRs. |
| API contract | Responses validate against the OpenAPI schema; problem-details envelope and pagination envelope are shape-tested ([08](08-api-principles.md)). |
| Migrations | Every migration runs **up and down** in CI against a real database ([10](10-deployment-and-operations.md#upgrades--migrations)). |
| Crash & recovery | Fault-injection proves the promised semantics: worker killed mid-job, duplicate result delivery, orchestrator restart, zombie write-back after lease expiry — at-least-once delivery, lease reclaim + fencing, idempotent result writes, cancellation of superseded jobs ([04](04-ingestion-pipeline.md), [05](05-extractor-contract.md#job-lifecycle)). Every operation type in the [12 § inventory](12-reliability.md#operation-inventory) has kill-and-restart coverage via the fault-injection harness ([F-001](../features/F-001-upload-and-import.md)'s "kill the importer mid-run" criterion, generalized); the `verify` audit ([12](12-reliability.md#verification)) runs clean after every such test. |
| Operations | The restore drill (backup → restore into a fresh stack → smoke suite; Q13) and the upgrade-path test (seed data on the previous tagged release → upgrade → smoke) run scheduled / at release — expand–contract proven in practice, not only up/down. |
| Search quality & performance | The golden-query benchmark (Q8) runs as a regression suite over the ground-truth corpus with declared metrics and thresholds — ranking changes are measured, never guessed. The same runs are the `benchmark` verification for statistical and performance FRs (e.g. [F-002/FR-10](../features/F-002-hybrid-search.md), latency on reference hardware — Q27). |

### Test infrastructure

- **Ground-truth corpus** — a versioned fixture set with a machine-readable manifest of each fixture's truth: PDFs with known phrases on known pages, images with known objects, audio/video with utterances at known timestamps — plus the adversarial set: unicode and case-colliding names (Q25), symlink layouts (Q22), zip-slip archive entries, corrupt / zero-byte / oversized files. One corpus feeds FR tests, the golden-query benchmark, and the conformance kit. Fixtures must be redistributable (Q28).
- **Reference extractor** — deterministic and instant, shipped with the conformance kit. Triple duty: E2E test double, executable example for third-party extractor authors, and the image the kit validates itself against.
- **Mutation testing** *(later)* — scheduled and scoped to the security-critical core (authz, permission-aware search, share links) as a machine check on test quality (Q29). Deliberately post-v1.

### Coverage

- The primary number is **coverage of requirements** — the matrix's share of FRs verified by their declared method; line coverage is the floor beneath it.
- CI gate: **≥ 85 % line coverage** project-wide. The gate only ratchets up — coverage never decreases.
- Security-critical code (authz checks, permission-aware search, share links, token handling) is **not** satisfied by a number: exhaustive tests including negative cases, reviewed as such.
- Coverage is a floor, not a target — tests exist to catch regressions, not to color lines green.

## Definition of Done

A task is done when every line is honestly true — or explicitly marked N/A in the MR:

- [ ] Feature file exists / is updated; acceptance criteria & FRs demonstrably met — **by tests, not by hand**; the traceability matrix is green for every touched FR.
- [ ] Tests added/changed/removed to match the behaviour; full suite green in CI; coverage gate met. New/changed tests **fail when their requirement is violated** (spot-checked in review); "never/only" guarantees have negative tests.
- [ ] Shared layer checked before anything new was built; extractions follow the [four tests](#when-to-abstract--four-tests); required refactors done in scope — no quick fixes, no unrecorded debt ([reuse](#code-reuse--shared-modules), [refactor over quick fix](#refactor-over-quick-fix)).
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
- 12-factor throughout: config from the environment; stateless processes (state lives in PostgreSQL); logs to stdout ([10](10-deployment-and-operations.md#logging)); graceful shutdown on SIGTERM — which for hours-long extraction jobs means re-queue, at-least-once delivery, idempotent result writes ([04](04-ingestion-pipeline.md), [05](05-extractor-contract.md#job-lifecycle)). SIGTERM handling is an optimization only — `kill -9` must be exactly as safe ([ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md), [12](12-reliability.md)).

## CI pipeline (the enforcement list)

All blocking: lint + format check · type check · unit tests · integration tests (real PostgreSQL + pgvector) · migrations up/down · fault-injection suite · **traceability matrix gate** · spec lint (FR format; vague-word warnings) · extractor conformance kit (official extractors) · E2E headless · coverage gate (≥ 85 %, ratcheting) · OpenAPI schema in sync + generated clients not stale · commit-format check · secret scan · dependency vulnerability scan · image scan (core + official extractor images) · SBOM generation.

Scheduled / release-gating rather than per-MR: benchmark suite against its thresholds (Q8, Q27) · upgrade-path test from the previous tagged release · restore drill (once Q13 resolves) · mutation run on the authz core (later, Q29).

Advisory (non-blocking): copy-paste/duplication report — input to the [reuse check](#code-reuse--shared-modules) in review, not a gate.
