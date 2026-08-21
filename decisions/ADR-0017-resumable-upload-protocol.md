# ADR-0017 — Uploads speak the IETF resumable-upload protocol, implemented in-app

**Status:** Accepted
**Date:** 2026-08-20

## Context

[F-001/FR-2](../features/F-001-upload-and-import.md) requires resumable uploads — an interrupted multi-GB video must resume, not restart — but no wire format was ever chosen (Q38). Four forces decide it:

1. **The API is the only interface** ([08](../specs/08-api-principles.md)). Web UI, future mobile apps, and future sync clients all upload through one path; a second upload dialect would be a second thing to specify, fault-inject, and keep correct.
2. **iOS background backup drives the server directly.** Apple's Photos background-upload extension (`PHBackgroundResourceUploadExtension`, iOS 26.1, already superseded by `PHBackgroundResourceUploadJobExtension` in iOS 27 — same protocol) hands the upload to the operating system, which speaks the **IETF Resumable Uploads protocol** and nothing else. It demands two concrete server behaviors: an `OPTIONS` preflight answered `200` with `Upload-Limit` (a non-supporting server answers `501`), and a `104 (Upload Resumption Supported)` interim response carrying `Location` while the body streams — Apple's documentation calls the 104 "the authoritative signal". This is the mechanism that makes [F-021](../features/F-021-mobile-auto-upload.md) reliable on iOS, and retrofitting the protocol later would be a breaking change for every already-shipped client.
3. **The protocol is stable enough to build on.** `draft-ietf-httpbis-resumable-upload-12` (2026-07-06) is past working-group last call, pre-IESG, and declares **interop version 9**; the editors reported at IETF 126 that recent changes "have not affected interop". The wire format we implement is the one the RFC will carry.
4. **No library exists.** There is no Python or ASGI implementation of the draft (only tus v1.0 servers), which [ADR-0012](ADR-0012-python-fastapi-core-stack.md) already accepted: the upload protocol is ours to write.

## Decision

We will implement the **IETF Resumable Uploads protocol as the only upload wire format**, in-app.

1. **Wire surface** (draft-12 semantics): upload creation by `POST` to the target collection carrying `Upload-Complete` (and `Upload-Length` where known) · an interim `104` with `Location` and `Upload-Limit` · `HEAD` on the upload resource reporting `Upload-Offset` · `PATCH` append with `Content-Type: application/partial-upload` at the declared offset, answering `409` plus the current `Upload-Offset` on mismatch · `DELETE` to cancel · `OPTIONS` advertising `Upload-Limit` (including `max-append-size`, so an intermediary's body limit is a published number rather than a mystery failure). A single request with `Upload-Complete: ?1` is a complete upload, so small files pay no extra round-trip.
2. **Version negotiation is one dialect table**, keyed on `Upload-Draft-Interop-Version`: **9** (draft-12, primary), **8** (drafts -09…-11, wire-identical for this flow), **6** (iOS 18.1+/macOS 15.1+ `URLSession`, the dialect tusd also implements). A missing or unrecognized version means **no `104` and no upload resource** — the request is served as an ordinary, non-resumable upload, which is the fallback the draft itself mandates. When the RFC publishes and drops the header, the header-less mode is one more row in that table, not a rewrite.
3. **No tus v1.0.** Nothing in our client set needs it: the generated web client, our own mobile apps, and the Apple system uploader all speak this protocol or plain HTTP.
4. **Content integrity is ours, not the protocol's** — draft-12 deliberately removed integrity digests. Finalize computes the content hash over the assembled bytes, verifies it against a client-declared hash when one was supplied (mismatch fails the upload without publishing a file), and records the computed hash as the `FileVersion` identity ([02](../specs/02-domain-model.md#fileversion)).
5. **The upload session is an operation record** ([12](../specs/12-reliability.md#filesystem-write-protocol)): staging accumulates in the workspace's `.workspace/staging/` area ([ADR-0018](ADR-0018-workspace-layout-and-adoption.md)), each append is fsync'd before its offset is committed, resume truncates staging to the committed offset, and an abandoned session expires and is janitor-collected. Finalize is hash-verify → atomic rename into place → `FileVersion` + extraction jobs + event in one transaction.

## Consequences

- iOS background backup works against a stock instance with no second protocol and no vendor relay, and the same endpoint serves the web UI's resumable uploads.
- **Interop churn is a table, not a migration.** Apple ships exactly one interop version per OS release (3 → 5 → 6 observed through iOS 18.1); the version current iOS sends is not published and must be captured from a real device before we claim support for it. An unknown version degrades to a plain upload instead of failing.
- We own an upload module and its conformance tests. The public tus conformance tester is pinned at draft -02 and cannot validate us; our own test suite is the only check that the dialects stay honest.
- **The protocol constrains the edge**, which becomes an operator requirement ([10](../specs/10-deployment-and-operations.md#edge-vs-app-responsibilities)): the proxy must forward `1xx` interim responses (Traefik's default proxy does; its experimental fast proxy does not), must not buffer request bodies, and must not cut the request short — Traefik's `readTimeout` defaults to 60 s and covers the entire body, which kills every large upload until it is raised.
- Chunk size is negotiable via `Upload-Limit` rather than hardcoded, so a proxy with a small body limit is configuration, not a redesign.
- The draft may still change before publication. We pin behavior per interop version and treat a new version as an additive row; we do not track drafts that no shipped client speaks (interop 3–5 are not implemented).
