import type { Reporter } from 'vitest/node';
import type { TestCase, TestModule, TestSuite } from 'vitest/node';

import { nodeId, RequirementReport, type Outcome } from './report.ts';

/**
 * Vitest → the requirement report ([11 § requirement traceability](../../../specs/11-engineering-standards.md#requirement-traceability-the-matrix)).
 *
 * Component and unit tests declare the requirement they verify with a tag:
 *
 *     it('announces the error to assistive technology', { tags: ['@F-027/FR-8'] }, () => …)
 */

const STATES: Record<string, Outcome> = {
  passed: 'passed',
  failed: 'failed',
  skipped: 'skipped',
  pending: 'not run',
};

export default class VitestRequirementReporter implements Reporter {
  private readonly report: RequirementReport;

  constructor(options: { layer?: string; outputFile?: string } = {}) {
    this.report = new RequirementReport(
      options.layer ?? 'web',
      options.outputFile ?? 'traceability-report.web.json',
    );
  }

  onTestCaseResult(testCase: TestCase): void {
    const titles: string[] = [];
    for (
      let node: TestSuite | TestModule = testCase.parent;
      node.type === 'suite';
      node = node.parent
    )
      titles.unshift(node.name);
    titles.push(testCase.name);

    this.report.record(
      nodeId(testCase.module.moduleId, titles),
      testCase.tags,
      STATES[testCase.result().state] ?? 'error',
    );
  }

  onTestRunEnd(): void {
    this.report.write();
  }
}
