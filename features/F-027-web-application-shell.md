# F-027 — Web Application Shell

**Status:** Draft
**Priority:** P0
**Clients:** web — this *is* the web client's frame; the native apps have their own ([F-019](F-019-mobile-connection.md), [F-025](F-025-client-parity.md))
**Depends on:** F-001, F-015
**Related specs:** [08-api-principles](../specs/08-api-principles.md), [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md#tokens--credentials), [10-deployment-and-operations](../specs/10-deployment-and-operations.md#topology), [11-engineering-standards](../specs/11-engineering-standards.md#code-reuse--shared-modules), [ADR-0014](../decisions/ADR-0014-vue-frontend-stack.md)

## Summary

The web client's frame: the thing that serves the app, signs a person in, holds the navigation around every other surface, turns a `problem+json` into a sentence, and documents the API to the person operating it. Everything the app can *do* is specified by the feature that owns it — browsing is [F-015](F-015-folders.md), uploading is [F-001](F-001-upload-and-import.md) — and this feature is what makes any of it reachable from a browser. It exists as its own file for two reasons: the frame has obligations no other feature would claim (how an expired session is discovered, what a request that fails looks like, whether the app can be framed by another site), and [F-025/FR-1](F-025-client-parity.md)'s parity bar is meaningless until "the web feature set" is written down.

## User stories

- As a user, I want to open my instance's URL, log in, and be where I left off.
- As a user, I want a failed action to tell me what happened in words, not a status code.
- As a user whose session expired while I was reading, I want to log in again and continue — not to lose what I was doing to a wall of errors.
- As the person running this instance, I want interactive API documentation behind my own login, so I can drive the API without hunting for a schema file.
- As a developer, I want one place where transport, errors and layout are decided, so a new surface is a component rather than a small application.

## Functional requirements

- **FR-1** **Served by the API container, same origin:** the built SPA is served by the same service that serves `/api/v1` ([10 § topology](../specs/10-deployment-and-operations.md#topology)). API paths (`/api/v1/*`, `/healthz`, `/readyz`) keep their responses; every other path returns the SPA's entry document so a deep link survives a reload. Hashed asset files are served immutable; the entry document is served `no-store`, so a deployed change is picked up without a cache purge. An image built without the client still starts and still serves the API — the absence is logged once, never fatal.
- **FR-2** **One security policy for the app origin:** every SPA response carries a `Content-Security-Policy` that permits scripts only from this origin and forbids framing, plugins, and form submission elsewhere. No script, style, font or image is fetched from a third-party host — an instance on a private network must work with no egress at all. This is the same-origin cookie's other half: the origin holding the session must not be able to run someone else's JavaScript.
- **FR-3** *(negative space)* **The app never sends a credential anywhere but its own origin, and never stores one.** No token, password or session value is written to `localStorage`, `sessionStorage`, `IndexedDB` or a URL; the session is the `HttpOnly` cookie and nothing else ([07 § tokens & credentials](../specs/07-identity-permissions-sharing.md#tokens--credentials)). Verified by a test that drives login and then inspects every client-side store.
- **FR-4** **Login:** email and password to `POST /auth/login`; the session is resolved on boot with `GET /auth/me`. A wrong credential shows the server's own message and does not distinguish "no such user" from "wrong password" ([07 § abuse protection](../specs/07-identity-permissions-sharing.md)). While a login is in flight the form cannot be submitted twice.
- **FR-5** **Every surface except login requires a resolved identity**, decided before the surface renders rather than after it fails. An unauthenticated visit to any path lands on login and returns to the requested path once signed in.
- **FR-6** **A `401` from any request ends the session in the client**: the cached identity and all cached server state are dropped, and the app returns to login with the path remembered. An expired session is an ordinary event, not an error state — no surface shows a stack of failures because the cookie aged out.
- **FR-7** **Logout** (`POST /auth/logout`) clears every cache the client holds, so no data from one account can be read by the next person at that browser.
- **FR-8** **Failures are rendered as prose from `problem+json`**: `title` and `detail` are shown, field-level `errors` are attached to their fields by `pointer`, and the `instance` (request id) is shown where a person could be asked to quote it. A response the client cannot parse still produces a sentence naming what failed, never an empty screen.
- **FR-9** **Interactive API documentation** at an authenticated route, rendering `GET /api/v1/openapi.json` with request execution against this instance ([08](../specs/08-api-principles.md)). It is absent when `SE_API_DOCS_ENABLED=false` — the schema route is gone and the client's link with it. The viewer is bundled with the app; it loads nothing from a third-party host and needs no relaxation of [FR-2](#functional-requirements)'s policy.
- **FR-10** **The frame:** which instance, who is signed in, where you are, and a way out — present on every authenticated surface. Navigation reflects only what phase 1 has; no surface advertises a capability the API does not yet offer.
- **FR-11** **Server state goes through the generated client and one cache** ([ADR-0014](../decisions/ADR-0014-vue-frontend-stack.md)): no hand-written HTTP, and one query cache so a mutation invalidates rather than re-fetching by hand. Enforced by lint, not convention.
- **FR-12** **Keyboard and assistive-technology access to the shell:** every control the frame owns is reachable and operable by keyboard, focus is visible, the current surface is announced on navigation, and each page has one `h1`. Verified by an automated accessibility check over the shell's own surfaces.
- **FR-13** **Every shared component is in the showcase** with a story per state it supports ([11 § reuse](../specs/11-engineering-standards.md#code-reuse--shared-modules)) — the inventory is evidence of what exists, so the next surface composes instead of inventing.

## API surface

Adds none. Consumes `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` · `GET /api/v1/openapi.json` · `GET /readyz`, plus the surfaces its screens belong to ([F-001](F-001-upload-and-import.md), [F-015](F-015-folders.md)). Serving the client is not an API route: it is the fallback under every path `/api/v1` does not claim.

## Out of scope

Anything a later phase's API does not yet answer: search ([F-002](F-002-hybrid-search.md)), tags ([F-003](F-003-tagging.md)), sharing and permission UI ([F-008](F-008-sharing-and-public-links.md)), the trash page and in-app delete ([F-014](F-014-deletion-and-trash.md)), live updates ([F-012](F-012-live-updates.md)), the offline cache and service worker ([F-026](F-026-offline-cache-and-prefetch.md), phase 6), viewers and previews ([F-017](F-017-views.md), [09](../specs/09-previews.md)), and the admin surfaces beyond what phase 1 exposes. Theming, internationalisation and a public share-page renderer are all later.

## Open questions

None of its own. [Q46](../OPEN-QUESTIONS.md) (mobile UI strategy) asks whether these surfaces are later embedded in the native apps; nothing here precludes it.

## Acceptance criteria

- **AC-1** (FR-1) A deep link to a client route returns the entry document with `Cache-Control: no-store` and the app then renders that route; a request to an API path is answered by the API, not the SPA; an asset under the hashed asset path is served immutable. An image with no built client answers the API normally and logs the absence once.
- **AC-2** (FR-2, FR-9) Every SPA response carries a `Content-Security-Policy` permitting scripts only from `'self'` and forbidding framing; loading the app and the docs page — including executing a request from the docs page — produces no policy violation and no request to any host but this instance's own.
- **AC-3** (FR-3) After a successful login, `localStorage`, `sessionStorage` and IndexedDB hold no value matching the password or the session cookie, and no request URL contains either.
- **AC-4** (FR-4, FR-5) An unauthenticated visit to a protected path lands on login; signing in returns to that path. A wrong password shows the server's message and leaves the user on the form; the submit control is disabled while the request is in flight.
- **AC-5** (FR-6, FR-7) A request answered `401` mid-session returns the app to login with the path remembered, and the surface shows no error stack. After logout, going back in the browser reveals no data from the previous session.
- **AC-6** (FR-8) A `422` from a form is shown per field against its `pointer`; a `409` is shown as its `detail`; a `500` names the request id. A malformed error body still yields a sentence.
- **AC-7** (FR-9) The docs route lists the API's operations and executes one against this instance, authenticated by the session cookie. With `SE_API_DOCS_ENABLED=false` the route is absent from the frame and the schema endpoint answers `404`.
- **AC-8** (FR-12) An automated accessibility check over login, the frame and each phase-1 surface reports no violations; the whole of a surface can be operated from the keyboard with focus visible throughout.
- **AC-9** (FR-10, FR-13) Every component under the shared layer has a story; the frame names the signed-in user and offers logout on every authenticated surface.
