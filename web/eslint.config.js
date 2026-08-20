import js from '@eslint/js';
import configPrettier from 'eslint-config-prettier';
import pluginVue from 'eslint-plugin-vue';
import globals from 'globals';
import tseslint from 'typescript-eslint';

/**
 * The reuse standard, enforced (11-engineering-standards.md § enforcement, ADR-0014):
 * one HTTP path, one shared layer, and dependencies that point one way.
 */

const NO_HAND_ROLLED_HTTP = {
  name: 'fetch',
  message: 'Call the API through @store-everything/api-client — never hand-rolled HTTP.',
};

const NO_AXIOS = {
  name: 'axios',
  message: 'Call the API through @store-everything/api-client — never hand-rolled HTTP.',
};

const FEATURES_ARE_OFF_LIMITS_TO_SHARED = {
  group: ['@/features/*', '**/features/*'],
  message: 'Dependencies point one way: the shared layer never imports from a feature.',
};

export default tseslint.config(
  {
    ignores: ['dist', 'coverage', 'storybook-static', 'playwright-report', 'test-results'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: { parser: tseslint.parser },
    },
    rules: {
      'no-restricted-globals': ['error', NO_HAND_ROLLED_HTTP, 'XMLHttpRequest'],
      'no-restricted-imports': ['error', { paths: [NO_AXIOS] }],
    },
  },
  {
    // Raw interactive elements belong in the shared layer, where focus handling,
    // keyboard behaviour and tokens are solved once.
    files: ['src/features/**/*.vue', 'src/App.vue'],
    rules: {
      'vue/no-restricted-html-elements': [
        'error',
        {
          element: 'button',
          message: 'Use the shared AppButton so focus, keyboard and tokens stay solved once.',
        },
        {
          element: 'dialog',
          message: 'Use the shared dialog primitive rather than a raw <dialog>.',
        },
      ],
      'vue/no-restricted-static-attribute': [
        'error',
        { key: 'style', message: 'Style through design tokens and utility classes, not inline.' },
      ],
    },
  },
  {
    files: ['src/shared/**/*.{ts,vue}'],
    rules: {
      'no-restricted-imports': [
        'error',
        { paths: [NO_AXIOS], patterns: [FEATURES_ARE_OFF_LIMITS_TO_SHARED] },
      ],
    },
  },
  {
    // The one place allowed to configure transport.
    files: ['src/shared/api/**/*.ts'],
    rules: {
      'no-restricted-globals': 'off',
    },
  },
  configPrettier,
);
