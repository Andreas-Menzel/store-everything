# ADR-0003 — Files on disk are the source of truth; all app data is a derived layer

**Status:** Accepted
**Date:** 2026-07-29

## Context

Requirement: "the app should handle files as if the app can be removed at any time." Users import an existing folder hierarchy, keep using it, and must never be locked in. v1 storage is a mounted folder (e.g. external NAS share) that the app treats as local. This rules out architectures where the app owns an opaque blob store and the hierarchy exists only in a database.

## Decision

We will keep **user files as plain files in the user's own hierarchy on mounted storage** — that tree *is* the data. Everything the app produces lives in two separated, app-owned places: **PostgreSQL** (domain model, index, tags, permissions) and a **derived store** directory (previews, keyframes, transcripts, version history). Rules:

1. Originals are never modified, re-encoded, or relocated except by explicit user file operations. OCR/extraction output is stored as derived data, never written back into files.
2. The visible hierarchy in the app mirrors the real one on disk (per workspace subtree).
3. Everything derived is regenerable by reprocessing, except manual input (manual/confirmed/rejected tags, users, permissions, shares), which is protected by ordinary DB backup.
4. Changes made directly on disk (outside the app) are legitimate and reconciled by re-scan, not treated as corruption.
5. **Versioning compromise**: the current version of every file is the real file at its real path; *superseded* versions are preserved in the app-owned `versions/` area (content-addressed). Removing the app loses history, never current data — documented, accepted.

## Consequences

- True portability and user trust; import of existing structures is trivial by design.
- No cross-user physical dedup (two identical uploads = two files on disk) — consistent with the domain rule that a file belongs to exactly one workspace; content-hash reuse of *extraction results* remains an internal optimization.
- The app must handle concurrent external modification of its "storage backend" (hashing + reconciliation instead of assuming exclusive ownership).
- Filesystem semantics (case sensitivity, path length, NAS mount latency) leak into the app and must be handled deliberately.
