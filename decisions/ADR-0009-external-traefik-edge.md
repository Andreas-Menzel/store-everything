# ADR-0009 — Edge via an existing external Traefik instance

**Status:** Accepted
**Date:** 2026-07-30

## Context

The app is deployable on a private home machine or a public server ([00](../specs/00-vision-and-goals.md), [07](../specs/07-identity-permissions-sharing.md)). Either way the deployment needs TLS termination, HTTP→HTTPS redirect, HSTS, and absorption of volumetric abuse — classic edge concerns that do not belong in the application. Our target environments already run a reverse proxy, and that proxy is **Traefik**. Bundling our own proxy would duplicate certificate management and fight over ports 80/443 with the proxy that is already there.

## Decision

v1 assumes an **existing, externally managed Traefik instance** as the edge. The app's Docker Compose attaches **only the core API container** to Traefik's shared external Docker network and carries Traefik router labels. Everything else — orchestrator, PostgreSQL, extractors — stays on the app-internal network, unreachable from the edge.

Traefik owns: TLS termination and certificates, HTTP→HTTPS redirect, HSTS, and volumetric/DDoS-class rate limiting. The core API serves **plain HTTP on the internal network only** and trusts `X-Forwarded-*` headers exclusively from the proxy network — the forwarded client IP feeds app-level rate limiting ([07](../specs/07-identity-permissions-sharing.md)) and the audit trail ([F-011](../features/F-011-audit-trail.md)).

The app stays **proxy-agnostic**: nothing in it depends on Traefik specifics; any reverse proxy works by translating the shipped labels. Traefik is the documented, first-class path.

## Consequences

- No certificates, no host ports 80/443, no TLS config in the app's compose file — `docker compose up` next to an existing Traefik just works.
- Deployment docs and the shipped compose file are written for Traefik labels; non-Traefik users translate them (documented as supported, not first-class).
- The app must reject `X-Forwarded-*` from anywhere but the proxy network — a spoofed client IP would poison rate limits and audit records.
- Local development needs no proxy (localhost bind, plain HTTP).
- Application-level concerns stay in the app: per-token/per-IP rate limits, CORS, content security headers ([08](../specs/08-api-principles.md), [10](../specs/10-deployment-and-operations.md)).
