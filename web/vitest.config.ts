import { mergeConfig } from 'vite';
import { defineConfig } from 'vitest/config';

import viteConfig from './vite.config.ts';
import { requirementTags } from './tools/traceability/requirements.ts';
import VitestRequirementReporter from './tools/traceability/vitest.ts';

/**
 * The test configuration, kept apart from `vite.config.ts` on purpose.
 *
 * Vitest will not accept a tag a test uses unless the configuration declares it, and the
 * declarations are read from the feature files ([Q59](../OPEN-QUESTIONS.md)). The production build
 * must not depend on those documents being present — the image's web stage copies `web/` and
 * `packages/`, not `features/` — so the reading happens here, in the file only the test run loads.
 */
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      tags: requirementTags(),
      reporters: ['default', new VitestRequirementReporter()],
    },
  }),
);
