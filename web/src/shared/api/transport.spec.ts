import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { ESLint } from 'eslint';
import { describe, expect, it } from 'vitest';

/**
 * [F-027/FR-11](../../../../features/F-027-web-application-shell.md): server state goes through
 * the generated client and one cache — **enforced by lint, not convention**.
 *
 * So the requirement is about the *gate*, and this is the test that proves the gate bites: it
 * hands eslint the code a reviewer would otherwise have to catch by eye. Without this, "enforced
 * by lint" is a claim about a config file nobody has seen reject anything.
 */

/** The web package root: where the eslint configuration lives. */
const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');

async function complaintsAbout(code: string, filePath: string): Promise<string[]> {
  const [result] = await new ESLint({ cwd: WEB_ROOT }).lintText(code, { filePath });
  return (result?.messages ?? []).map((message) => message.message);
}

describe('hand-rolled HTTP', () => {
  it('is rejected in a feature', { tags: ['@F-027/FR-11'] }, async () => {
    const complaints = await complaintsAbout(
      "export const load = () => fetch('/api/v1/files');\n",
      join(WEB_ROOT, 'src/features/probe/load.ts'),
    );

    expect(complaints.join(' ')).toContain('never hand-rolled HTTP');
  });

  it('is rejected as an XMLHttpRequest too', { tags: ['@F-027/FR-11'] }, async () => {
    const complaints = await complaintsAbout(
      'export const load = () => new XMLHttpRequest();\n',
      join(WEB_ROOT, 'src/features/probe/load.ts'),
    );

    expect(complaints.join(' ')).toContain('XMLHttpRequest');
  });

  it('is rejected as an axios import', { tags: ['@F-027/FR-11'] }, async () => {
    const complaints = await complaintsAbout(
      "import axios from 'axios';\nexport const client = axios;\n",
      join(WEB_ROOT, 'src/features/probe/load.ts'),
    );

    expect(complaints.join(' ')).toContain('never hand-rolled HTTP');
  });

  it(
    'is allowed in the one module that configures transport',
    { tags: ['@F-027/FR-11'] },
    async () => {
      // The exception is deliberate and narrow: `shared/api` is where the interceptor lives, and
      // a rule with no exception would be worked around instead of obeyed.
      const complaints = await complaintsAbout(
        "export const raw = () => fetch('/readyz');\n",
        join(WEB_ROOT, 'src/shared/api/probe.ts'),
      );

      expect(complaints).toEqual([]);
    },
  );
});
