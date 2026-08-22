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
  build: {
    // The documentation viewer is a deliberate 1.4 MB chunk of its own, loaded only on its route
    // (F-027/FR-9). A permanent warning about an intended cost trains people to ignore output.
    chunkSizeWarningLimit: 1600,
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
