import { readdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * [F-027/FR-13](../../../../features/F-027-web-application-shell.md): the showcase is the
 * inventory of what exists.
 *
 * The point is not documentation for its own sake — it is that the next surface *composes*
 * instead of inventing a fourth button ([11 § reuse](../../../../specs/11-engineering-standards.md#code-reuse--shared-modules)).
 * A shared component nobody can see is a component the next person writes again, so a missing
 * story is a real defect and this is the test that finds it.
 */

const UI_DIR = dirname(fileURLToPath(import.meta.url));

describe('the shared component showcase', () => {
  const files = readdirSync(UI_DIR);
  const components = files.filter((name) => name.endsWith('.vue'));

  it('covers every shared component', { tags: ['@F-027/FR-13'] }, () => {
    expect(components.length).toBeGreaterThan(0);

    const undocumented = components
      .map((component) => component.replace(/\.vue$/, ''))
      .filter((name) => !files.includes(`${name}.stories.ts`));

    expect(undocumented).toEqual([]);
  });

  it('has no story without a component behind it', { tags: ['@F-027/FR-13'] }, () => {
    const orphans = files
      .filter((name) => name.endsWith('.stories.ts'))
      .map((story) => story.replace(/\.stories\.ts$/, ''))
      .filter((name) => !components.includes(`${name}.vue`));

    expect(orphans).toEqual([]);
  });
});
