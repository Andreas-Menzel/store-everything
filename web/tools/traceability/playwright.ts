import type { Reporter, Suite, TestCase, TestResult } from '@playwright/test/reporter';

import { nodeId, RequirementReport, type Outcome } from './report.ts';

/**
 * Playwright → the requirement report ([11 § requirement traceability](../../../specs/11-engineering-standards.md#requirement-traceability-the-matrix)).
 *
 * Browser tests declare the requirement they verify with Playwright's own tag option:
 *
 *     test('a 401 mid-session returns to login', { tag: ['@F-027/FR-6'] }, async ({ page }) => …)
 *
 * A tag on a `describe` is inherited by everything inside it, which is how a whole surface's
 * accessibility scan can be attributed to one requirement without repeating the id per test.
 */

const STATUSES: Record<TestResult['status'], Outcome> = {
  passed: 'passed',
  failed: 'failed',
  timedOut: 'failed',
  interrupted: 'error',
  skipped: 'skipped',
};

export default class PlaywrightRequirementReporter implements Reporter {
  private readonly report: RequirementReport;

  constructor(options: { layer?: string; outputFile?: string } = {}) {
    this.report = new RequirementReport(
      options.layer ?? 'web-e2e',
      options.outputFile ?? 'traceability-report.e2e.json',
    );
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    // Only the `describe` titles: the project and file levels are already in the path.
    const titles: string[] = [];
    for (let node: Suite | undefined = test.parent; node !== undefined; node = node.parent)
      if (node.type === 'describe') titles.unshift(node.title);
    titles.push(test.title);

    this.report.record(nodeId(test.location.file, titles), test.tags, STATUSES[result.status]);
  }

  onEnd(): void {
    this.report.write();
  }
}
