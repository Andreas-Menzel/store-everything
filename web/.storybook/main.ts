import type { StorybookConfig } from '@storybook/vue3-vite';

/**
 * The living component inventory — and the first stop before building anything new
 * (11-engineering-standards.md § where to look).
 */
const config: StorybookConfig = {
  stories: ['../src/**/*.stories.ts'],
  framework: {
    name: '@storybook/vue3-vite',
    options: {},
  },
  // This project makes no unrequested network calls — that applies to its tooling too
  // (00-vision-and-goals.md § security posture).
  core: {
    disableTelemetry: true,
  },
};

export default config;
