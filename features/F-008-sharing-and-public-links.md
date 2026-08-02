# F-008 — Permissions, Sharing & Public Links

**Status:** Draft
**Priority:** P1
**Clients:** all
**Depends on:** F-001
**Related specs:** [07-identity-permissions-sharing](../specs/07-identity-permissions-sharing.md)

## Summary

Owners grant other users roles (`read`/`write`/`manage`) on workspaces, folder subtrees, or single files — enabling shared truth on tags and content. Grantees see what they received under **Shared with me**: each grant surfaces as its own root, disclosing nothing about its location in the owner's hierarchy ([07 § visibility roots](../specs/07-identity-permissions-sharing.md#visibility-roots-what-a-grantee-sees)). Public share links give account-less visitors download/preview access to a specific file, with expiry, optional password, and revocation. All permission checks are enforced at the API layer and inside search.

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
- **FR-8** Permission changes take effect immediately (next request) — including search visibility and archive downloads ([F-016/FR-5](F-016-archive-download.md)).
- **FR-9** A link whose target is trashed is **suspended**, not revoked: it stops serving on trash, resumes on restore, and is permanently revoked on purge ([F-014/FR-11](F-014-deletion-and-trash.md); suspended-response status code is Q21).
- **FR-10** **Shared with me:** `GET /shared-with-me` lists the caller's visibility roots ([07 § visibility roots](../specs/07-identity-permissions-sharing.md#visibility-roots-what-a-grantee-sees)) received from other users' grants — **topmost roots only** (a root contained in another listed root is not listed separately), folders and single files alike, each with the granting owner and the caller's effective role. The listing discloses nothing about a root's location in the owner's hierarchy ([F-015/FR-12–13](F-015-folders.md)).
- **FR-11** Shared with me is a reserved navigation entry with per-user `hidden`/`position` state ([F-017/FR-7](F-017-views.md)); folder roots navigate into browse ([F-015](F-015-folders.md)), file roots open the file.

## API surface

`GET/POST/DELETE /permissions` (scoped) · `GET /shared-with-me` (received grant roots) · `GET/POST/DELETE /shares` · `GET /shares/{token}` (public, unauthenticated) · audit via `GET /audit`.

## Out of scope

Groups/teams (grants are per-user in v1). Folder-level public links (later). Deny rules. External identity providers.

## Open questions

[Q12 (upload links / file-request links?)](../OPEN-QUESTIONS.md#q12) · [Q21 (response code for suspended links)](../OPEN-QUESTIONS.md).

## Acceptance criteria

- Bob without grants: Alice's file absent from his listings, search, and direct fetch (404/403).
- After `read` grant: file findable by Bob in search; after revocation: immediately gone again.
- Expired or revoked share link returns 410-style response; password-protected link requires the password.
- Share link token grants access to exactly one file — crafted requests reach nothing else.
- (FR-10, FR-11) Alice grants Bob `read` on folder `Acme` and on the nested folder `Acme/Contracts`, plus `write` on one file elsewhere: Bob's `GET /shared-with-me` lists exactly two roots — `Acme` (read, from Alice) and the single file (write, no parent reference) — never `Contracts` separately; the entry disappears with the next request after revocation.
