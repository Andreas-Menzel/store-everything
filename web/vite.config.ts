import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vitest/config';

/** Paths the core service owns; in development Vite forwards them to it. */
const API_PATHS = ['/api', '/healthz', '/readyz'];
const DEV_API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // The app is served same-origin with the API in production, so requests are
    // relative and need no CORS entry (08-api-principles.md). The dev proxy keeps
    // that true while Vite serves the client.
    proxy: Object.fromEntries(
      API_PATHS.map((path) => [path, { target: DEV_API_TARGET, changeOrigin: false }]),
    ),
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,vue}'],
      exclude: ['src/**/*.stories.ts', 'src/main.ts'],
    },
  },
});
