/**
 * The requirement-coverage report, as the web test runners write it.
 *
 * The matrix is language-agnostic by design: the marker convention and the report shape are
 * specified once ([11 § requirement traceability](../../../specs/11-engineering-standards.md#requirement-traceability-the-matrix))
 * and each runner writes the same document. This module is the shape; `vitest.ts` and
 * `playwright.ts` are the two producers that fill it in, and `server/tools/fr_plugin.py` is the
 * third, for the core suite.
 *
 * Tags carry the ids. Playwright requires a tag to start with `@`, so every runner uses that
 * spelling — one convention rather than one per runner — and this module strips it.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/** `web/tools/traceability` → the repository root, so node ids are repo-relative everywhere. */
export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');

export const DEFAULT_METHOD = 'test';

/** Tags that declare a *method* rather than a requirement (11 § verification methods). */
export const METHOD_TAGS: Record<string, string> = {
  '@benchmark': 'benchmark',
  '@fault-injection': 'fault-injection',
  '@drill': 'drill',
};

/** `@F-027/FR-5`, and the domain invariants under their own stable ids (`@02/INV-6`). */
const REQUIREMENT_TAG = /^@(F-\d{3}\/FR-\d+|\d{2}\/INV-\d+)$/;

export type Outcome = 'passed' | 'failed' | 'skipped' | 'error' | 'not run';

/** Worst-wins, so a test that fails on a retry or in teardown is never reported as a pass. */
const OUTCOME_RANK: Record<Outcome, number> = {
  passed: 0,
  skipped: 1,
  'not run': 2,
  failed: 3,
  error: 4,
};

export interface Entry {
  requirements: string[];
  methods: string[];
  outcome: Outcome;
}

export function requirementsFrom(tags: readonly string[]): string[] {
  return tags.flatMap((tag) => {
    const match = REQUIREMENT_TAG.exec(tag);
    return match?.[1] === undefined ? [] : [match[1]];
  });
}

export function methodsFrom(tags: readonly string[]): string[] {
  const methods = tags.flatMap((tag) => {
    const method = METHOD_TAGS[tag];
    return method === undefined ? [] : [method];
  });
  return methods.length > 0 ? [...new Set(methods)].sort() : [DEFAULT_METHOD];
}

/** A path inside the repository, in the same shape a pytest node id has. */
export function nodeId(file: string, titles: readonly string[]): string {
  return `${relative(REPO_ROOT, file)}::${titles.filter(Boolean).join(' > ')}`;
}

/**
 * Accumulates one run's coverage and writes it out once, at the end.
 *
 * A test with no requirement tag is not recorded at all: **not every test carries a marker**
 * (11 § requirement traceability), and a report full of untagged tests would make the backward
 * gate meaningless.
 */
export class RequirementReport {
  private readonly tests = new Map<string, Entry>();

  constructor(
    private readonly layer: string,
    private readonly outputFile: string,
  ) {}

  record(id: string, tags: readonly string[], outcome: Outcome): void {
    const requirements = requirementsFrom(tags);
    if (requirements.length === 0) return;

    const existing = this.tests.get(id);
    if (existing === undefined) {
      this.tests.set(id, { requirements, methods: methodsFrom(tags), outcome });
      return;
    }
    if (OUTCOME_RANK[outcome] > OUTCOME_RANK[existing.outcome]) existing.outcome = outcome;
  }

  /** Written unconditionally, even for an empty run: a stale report read as a fresh one is how
   * a deleted test keeps its requirement green. */
  write(): void {
    const path = resolve(REPO_ROOT, this.outputFile);
    mkdirSync(dirname(path), { recursive: true });
    const tests = Object.fromEntries([...this.tests].sort(([a], [b]) => (a < b ? -1 : 1)));
    writeFileSync(path, `${JSON.stringify({ layer: this.layer, tests }, undefined, 2)}\n`, 'utf8');
  }
}
