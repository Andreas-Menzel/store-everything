# F-008 — Permissions, Sharing & Public Links

**Status:** Draft
**Priority:** P1
**Depends on:** F-001
**Related specs:** [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md)

## Summary

Owners grant other users roles (`read`/`write`/`manage`) on workspaces, folder subtrees, or single files — enabling shared truth on tags and content. Public share links give account-less visitors download/preview access to a specific file, with expiry, optional password, and revocation. All permission checks are enforced at the API layer and inside search.

## User stories

- As Alice, I want to give Bob write access to one file so that he can update its tags — and I see his updates.
- As a user, I want to send someone outside the instance a download link that expires next week.
- As an owner, I want to see and revoke everything I've shared.

## Functional requirements

- **FR-1** Grants: (user, role, scope) where scope ∈ workspace | folder subtree | file; effective permission is the union along the path.
- **FR-2** `read` → view/download/search-find; `write` → + new versions, edit tags/metadata, move within scope; `manage` → + grant, delete, create share links.
- **FR-3** Tag/metadata edits by grantees are visible to everyone with read (shared truth, stamped with editor's user id).
- **FR-4** Search visibility follows `read` exactly ([F-002/FR-7](F-002-hybrid-search.md)).
- **FR-5** Share links: unguessable token → download + preview of one file. No search, no metadata/tag browsing, no traversal.
- **FR-6** Share link options: expiry, password (hashed), revocation, access counter. All link accesses audited.
- **FR-7** Owners can list all grants and links they've issued (and admins instance-wide).
- **FR-8** Permission changes take effect immediately (next request) — including search visibility.

## API surface

`GET/POST/DELETE /permissions` (scoped) · `GET/POST/DELETE /shares` · `GET /shares/{token}` (public, unauthenticated) · audit via `GET /audit`.

## Out of scope

Groups/teams (grants are per-user in v1). Folder-level public links (later). Deny rules. External identity providers.

## Open questions

[Q12 (upload links / file-request links?)](../OPEN-QUESTIONS.md#q12).

## Acceptance criteria

- Bob without grants: Alice's file absent from his listings, search, and direct fetch (404/403).
- After `read` grant: file findable by Bob in search; after revocation: immediately gone again.
- Expired or revoked share link returns 410-style response; password-protected link requires the password.
- Share link token grants access to exactly one file — crafted requests reach nothing else.
