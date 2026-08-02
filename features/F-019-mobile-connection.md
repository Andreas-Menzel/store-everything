# F-019 — Mobile Apps: Connection & Device Sessions

**Status:** Draft
**Priority:** P1
**Clients:** Android, iOS — the native app shell itself; the QR pairing *display* side (FR-3) is a web-UI capability
**Depends on:** —
**Related specs:** [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md), [08-api-principles](../specs/08-api-principles.md), [13-mobile-clients](../specs/13-mobile-clients.md)

## Summary

The native apps' foundation: connect to a self-hosted instance by URL, authenticate, and hold a per-device session. Every device is a **named, scoped personal access token** ([07](../specs/07-identity-permissions-sharing.md#tokens--credentials)) — listed and revocable server-side like any other token, so a lost phone is one revocation from harmless. QR pairing removes credential typing on phone keyboards. The app stays useful offline (cached content readable) and fails loudly, never silently, when the server is unreachable.

## User stories

- As a user, I want to connect the app by scanning a QR code from my logged-in browser so that I never type my server URL and password on a phone.
- As a user, I want to see and revoke each of my devices in the token list so that a lost phone cannot keep accessing my data.
- As a user on a train, I want the app to show my cached library instead of erroring so that offline time isn't dead time.

## Functional requirements

- **FR-1** The app connects given a server base URL over HTTPS and authenticates via the standard auth endpoints; connection and auth failures surface the RFC 9457 problem's title and detail ([08](../specs/08-api-principles.md#errors-rfc-9457)) — never a generic "something went wrong". TLS trust beyond valid CA chains (self-signed home setups) is [Q40](../OPEN-QUESTIONS.md).
- **FR-2** Completing login creates a **device token**: a scoped personal access token named after the device (name user-editable). All app traffic authenticates with it; it appears in the account's token list; server-side revocation ends the session on the device's next request (401 → return to login, local credentials cleared).
- **FR-3** **QR pairing:** an authenticated web session can create a **one-time pairing code** (≥ 128 bit entropy, TTL ≤ 5 minutes, single-use) rendered as a QR embedding server URL + code; the app scans and exchanges it — unauthenticated, the code is the credential — for a device token. Creation and every exchange attempt are audited ([F-011](F-011-audit-trail.md)); the exchange endpoint is rate-limited like share-link password attempts ([07](../specs/07-identity-permissions-sharing.md#abuse-protection)).
- **FR-4** Logout revokes the device token server-side and removes credentials from the device. Removing cached and downloaded content ([F-020](F-020-mobile-library.md), [F-024](F-024-offline-files-and-downloads.md)) is a separate, explicit choice offered at logout — never implicit.
- **FR-5** Tokens and credentials are stored exclusively in the platform secure store (Android Keystore, iOS Keychain) — never in plaintext files, preferences, logs, or OS backups outside the secure store.
- **FR-6** *(negative space)* With the server unreachable, previously cached content remains browsable read-only, and every server-requiring action fails visibly with an offline notice. The app never fakes success and never silently discards an attempted action.
- **FR-7** Optional app lock: biometric/PIN via the platform authenticator, required after a configurable idle timeout; while locked, no library content is visible — including the OS task-switcher snapshot.
- **FR-8** v1 holds exactly one connected account at a time; switching accounts = logout + login. The connection model must not preclude multi-account/multi-server later ([Q39](../OPEN-QUESTIONS.md)).

## API surface

`POST /auth/login` · `GET/POST/DELETE /auth/tokens` (existing) — **new:** `POST /auth/pairing-codes` (authenticated: create one-time code) · `POST /auth/pairing` (public: exchange code → device token; joins the documented unauthenticated surface — [08](../specs/08-api-principles.md#endpoint-map-visual)).

## Out of scope

Multi-account / multi-server ([Q39](../OPEN-QUESTIONS.md)). Push notifications ([Q41](../OPEN-QUESTIONS.md)). SSO/OIDC (kept possible per [07](../specs/07-identity-permissions-sharing.md#users)).

## Open questions

[Q39 (multi-account)](../OPEN-QUESTIONS.md) · [Q40 (mobile TLS policy)](../OPEN-QUESTIONS.md) · [Q41 (push notifications)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- **AC-1** (FR-3) The web app shows a pairing QR; the app scans it and is signed in without typed credentials. A second exchange of the same code, and any exchange after 5 minutes, fails with a problem response; all attempts appear in the audit log.
- **AC-2** (FR-2) Revoking the device token in the web app: the app's next request returns 401, the app returns to login, and no credential material remains on the device.
- **AC-3** (FR-6) In airplane mode the app shows cached library content; adding a tag fails with a visible offline notice; nothing is retried silently after connectivity returns without user action.
- **AC-4** (FR-5) Inspecting the app's storage and an OS backup export finds no token material outside the platform secure store.
- **AC-5** (FR-7) With app lock enabled, reopening after the timeout requires unlock, and the task switcher shows no library content while locked.
