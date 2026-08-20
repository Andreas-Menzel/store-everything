# Web UI

The baseline client ([ADR-0014](../decisions/ADR-0014-vue-frontend-stack.md)): a Vue 3 SPA on
Vite, talking to the core service only through the generated client.
Run commands from the repository root (`make …`) or from here with pnpm.

```bash
pnpm install              # from the repository root: installs the whole workspace
pnpm run dev              # Vite dev server; /api, /healthz and /readyz proxy to :8000
pnpm run lint             # ESLint, including the shared-layer rules
pnpm run typecheck        # vue-tsc
pnpm run test             # Vitest component and composable tests
pnpm run e2e              # Playwright, headless (add --ui for the headed lens)
pnpm run storybook        # component showcase
pnpm run build            # production bundle
```

## Layout

| Path | Contains |
|---|---|
| `src/shared/` | The canonical shared layer: UI primitives, transport configuration. Never imports from a feature. |
| `src/features/` | Feature code. May import from `shared`, never the reverse. |
| `src/views/` | Route targets, assembled from features and shared pieces. |
| `src/styles/tokens.css` | The only place colours, spacing and radii are defined. |
| `e2e/` | Playwright specs. The API is stubbed at the network boundary so they assert our wiring, not a running instance. |

Lint enforces what the [reuse standard](../specs/11-engineering-standards.md#code-reuse--shared-modules)
requires rather than trusting anyone to remember it: no hand-rolled HTTP outside the generated
client, no raw `<button>`/`<dialog>` outside the shared layer, no inline styles, and no imports
from features into shared.

The API client in [`../packages/api-client`](../packages/api-client) is **generated** from the
committed `openapi.json` — run `make openapi` after any API change and commit the result.
