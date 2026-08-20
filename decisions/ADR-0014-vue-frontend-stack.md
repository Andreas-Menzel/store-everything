# ADR-0014 — Web UI stack: Vue 3 SPA with a generated API client

**Status:** Accepted
**Date:** 2026-08-20

## Context

[Q26](../OPEN-QUESTIONS.md) asked for the web UI's framework plus the tooling that [11 § code reuse](../specs/11-engineering-standards.md#code-reuse--shared-modules) needs made concrete: component approach, showcase tool, lint rules enforcing the shared layer and import direction, and the E2E "UI mode" tool. The reuse *rules* were already fixed and stack-agnostic; only their enforcement waited.

The web UI is the baseline client ([F-025](../features/F-025-client-parity.md)) and carries the full product surface: a virtualized timeline over a 100k-item library driven by a histogram ([F-017](../features/F-017-views.md), [F-002/FR-19–20](../features/F-002-hybrid-search.md)), faceted search, per-class viewers opening at positions (PDF pages, video timestamps), a map page ([Q35](../OPEN-QUESTIONS.md)), admin surfaces, live updates over WebSocket ([F-012](../features/F-012-live-updates.md)), and an offline cache with prefetch ([F-026](../features/F-026-offline-cache-and-prefetch.md), [14](../specs/14-client-sync-and-caching.md)).

A survey of 2026 options found three defensible frameworks and clear reasons against the rest. React 19 has the largest ecosystem and the deepest headless-accessibility primitives; Svelte 5 backs Immich, the closest comparable product; Vue 3 backs Nextcloud at far larger scale than this project targets. Solid was excluded (small ecosystem mid-2.0 replatform) and Angular (heaviest idiom overhead for a two-person team, weakest headless component ecosystem). Notably, the libraries that carry the hard parts of this UI — `maplibre-gl`, `hls.js`, `pdfjs-dist`, TanStack Virtual — are framework-agnostic or ship adapters for all three, so the choice does not gate any capability.

## Decision

We will build the web UI as a **Vue 3.5 single-page application on Vite**, with **no SSR framework** — the app is authenticated end to end, so server rendering buys nothing and costs a second runtime.

| Concern | Choice |
|---|---|
| Framework & build | **Vue 3.5** (pinned to the 3.5 line), **Vite**, TypeScript strict, `vue-tsc` in CI |
| Routing & state | `vue-router`; **Pinia** for client state; **TanStack Query (vue-query)** for all server state — caching, revalidation, and the invalidation target for [F-012](../features/F-012-live-updates.md) notifications |
| Components | **Reka UI** headless primitives as the base of the shared layer (Ark UI where it has no equivalent); **Tailwind 4** with design tokens defined once ([11 § reuse rule 5](../specs/11-engineering-standards.md#the-rules)) |
| Showcase | **Storybook 10** (vue3-vite) — the living component inventory the reuse standard requires |
| Tests | **Vitest** + Vue Testing Library for components; **Playwright** for E2E, headless in CI and headed in UI mode — the *same* tests ([11 § testing](../specs/11-engineering-standards.md#testing)) |
| API access | **hey-api** generates the typed client from the committed `openapi.json`; lint forbids `fetch`/`axios` outside the generated client ([11 § reuse rule 8](../specs/11-engineering-standards.md#the-rules)) |
| Virtualization | TanStack Virtual (or `virtua`) for list/grid windowing; the justified-timeline layout math is ours |
| Offline | Service worker + IndexedDB (Dexie) implementing [14](../specs/14-client-sync-and-caching.md)'s contract — hand-written, not a generated caching preset |
| Repo layout | **pnpm workspace**: `web/` (app) and `packages/api-client/` (generated); Python core beside it in the same repository |

**Enforcement is part of the decision** ([11 § enforcement](../specs/11-engineering-standards.md#enforcement)): ESLint rules forbid raw UI primitives and hardcoded colors/spacing outside the shared layer, forbid imports from features into the shared layer, and forbid hand-rolled HTTP. An unenforced convention is a suggestion that rots.

## Consequences

- **Vue 3.5 is pinned deliberately.** Vapor mode is not stable at decision time; adopting it is a later, isolated evaluation, not a default upgrade.
- **Framework-specific bindings are thinner than React's**, so occasionally we will wrap a framework-agnostic library ourselves. The libraries that matter most here are framework-agnostic anyway, which bounds this cost.
- **The timeline is custom work regardless of framework** — Immich wrote its own justified layout in WASM. Our scroll geometry rides the server-side histogram ([F-002/FR-19](../features/F-002-hybrid-search.md)) and will be measured against [F-020](../features/F-020-mobile-library.md)'s bars.
- **No SSR means no SEO story.** Correct for this product: every surface requires authentication, and public share pages render a deliberately minimal, no-store view ([08](../specs/08-api-principles.md)).
- **[Q46](../OPEN-QUESTIONS.md) (mobile UI strategy) stays open** but is no longer blocked on its counterpart: reusing web surfaces in embedded views now means embedding a Vue SPA, which is a concrete thing to evaluate in phase 6.
- **The API client is generated, never hand-written**, so an OpenAPI drift check in CI is what keeps the UI honest against the contract ([11 § CI](../specs/11-engineering-standards.md#ci-pipeline-the-enforcement-list)).
- Resolves [Q26](../OPEN-QUESTIONS.md).
