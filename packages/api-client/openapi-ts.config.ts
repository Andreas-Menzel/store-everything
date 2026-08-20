import { defineConfig } from '@hey-api/openapi-ts';

/**
 * The client is generated from the committed contract, never from a running server
 * (08-api-principles.md). Regenerate with `make openapi` after any API change; CI fails
 * if the checked-in output differs.
 *
 * The output is deliberately self-contained: nothing from the generator is needed at
 * runtime, so the web bundle carries no code-generation dependency.
 */
export default defineConfig({
  input: '../../openapi.json',
  output: {
    path: 'src/generated',
    postProcess: [],
  },
  plugins: ['@hey-api/client-fetch'],
});
