# 10 — Deployment and Operations

**Status:** Draft
**Related ADRs:** [ADR-0005](../decisions/ADR-0005-single-server-docker-network.md), [ADR-0009](../decisions/ADR-0009-external-traefik-edge.md)

## Topology

The whole app is one Docker Compose deployment on one server (ADR-0005), attached to an **existing, externally managed Traefik** (ADR-0009):

```mermaid
flowchart LR
    C["Clients"] -->|HTTPS| TR["Traefik<br/>(pre-existing, separate deployment)<br/>TLS · redirect · HSTS · volumetric limits"]

    subgraph app["App compose (this product) — internal network, no ingress"]
        API["Core API"]
        ORCH["Orchestrator"]
        PG[("PostgreSQL")]
        EX["Extractors"]
    end

    TR -->|"shared external<br/>Docker network"| API
    API --- PG
    ORCH --- PG
    ORCH --- EX
```

Rules:

1. **Only the core API joins the Traefik network.** Orchestrator, PostgreSQL, and extractors live on the internal network with no ingress; extractors additionally have no egress by default (Q7).
2. The API serves **plain HTTP internally**; TLS exists only at the edge.
3. `X-Forwarded-*` is trusted **only from the proxy network** — a spoofed client IP would poison rate limiting ([07](07-identity-permissions-sharing.md#abuse-protection)) and audit records ([F-011](../features/F-011-audit-trail.md)).
4. The app is **proxy-agnostic**: nothing depends on Traefik specifics; any reverse proxy works by translating the shipped labels. Traefik is the documented first-class path.
5. Local development runs without a proxy (localhost bind, plain HTTP).

## Edge vs. app responsibilities

| Concern | Lives in |
|---|---|
| TLS + certificate management | Traefik |
| HTTP→HTTPS redirect, HSTS | Traefik |
| Volumetric / DDoS-class rate limiting | Traefik |
| Per-token/per-IP rate limits; login & share-password brute-force protection | App ([07](07-identity-permissions-sharing.md#abuse-protection)) |
| CORS (deny by default, explicit allow-list) | App ([08](08-api-principles.md)) |
| Content security headers (`nosniff`, frame-deny) | App |

## Health & readiness

- `GET /healthz` — liveness: the process is up. Unauthenticated **by design** (one of the documented public exceptions — [08](08-api-principles.md)); reveals nothing (no version, no internals).
- `GET /readyz` — readiness: database reachable, migrations current. Used by compose healthchecks and Traefik's health check.

## Configuration & secrets

- 12-factor: **deployment config comes from the environment** — never hardcoded, never committed. The compose file reads a git-ignored `.env`; a committed `.env.example` documents every variable with dummy values. (Domain configuration — workspaces, extractors, users — is data and lives in PostgreSQL, [03](03-storage-and-portability.md).)
- **Secrets** (DB password, token-signing key, remote-extractor credentials) are env-provided, held in secret types in code, **never logged**, never baked into images or layers.
- Credentials for network-enabled extractors are per-extractor env config — never in the manifest ([05](05-extractor-contract.md#container-requirements-hardening), Q19).

## Logging

- Structured (JSON) to **stdout** only. The app never writes or rotates log files — persistence and retention are the platform's concern (Docker logging driver).
- Level is env-configurable, default `INFO` in production. Levels are used deliberately (`DEBUG` dev detail · `INFO` milestones · `WARNING` degraded · `ERROR` operation failed · `CRITICAL` service-threatening); log the cause, not just the symptom.
- Every request gets a **request id** ([08](08-api-principles.md#errors-rfc-9457)); every log line carries it — it is the only bridge between a client-visible error and the internal cause.
- **Logs never contain secrets, tokens, file contents, search queries, or result snippets.** File paths appear at `DEBUG` only. For a product whose pitch is local-first privacy, leaking user data into logs is a breach, not an inconvenience.

## Upgrades & migrations

- An upgrade is: pull new images → `docker compose up`. Release notes state the contained migrations and the support-matrix entry ([11](11-engineering-standards.md#versioning--releases)).
- Every schema change ships as a **versioned migration** committed with the code — never hand-run DDL against a live database.
- Migrations are **expand–contract**: the *previous* app version must run against the migrated schema, because rollback is "redeploy the previous image".
- Both directions (`up` **and** `down`) are tested in CI ([11](11-engineering-standards.md#testing)).
- *When* migrations execute (automatically at startup with a pre-flight dump vs. an explicit separate step) is open — Q20.

## Disk space

Disk exhaustion is an operational emergency the system must survive politely — never a reason to destroy data ([F-014/FR-9](../features/F-014-deletion-and-trash.md)):

- **Monitored volumes:** source tree(s), derived store, `versions/`, PostgreSQL. Admin-configurable warn/critical thresholds (proposed defaults: 90 % / 95 %) emit alert events surfaced to admins; email/push delivery arrives with the future notification feature.
- **When a volume is full:** operations that need space — uploads, version snapshots, trash safeguarding ([03](03-storage-and-portability.md#deletion--trash)), rendition/archive builds — fail with a documented out-of-space problem type ([08](08-api-principles.md#errors-rfc-9457)) naming the affected volume. Reads and search keep working.
- **Freeing space:** users empty their trash; admins may early-purge with typed confirmation (audited); cache-like derived kinds (PDF pages, renditions, archives) are LRU-evicted automatically — they are regenerable, trash is not. **Purge and empty-trash are never blocked by a full disk.**
- The system never deletes non-regenerable data on its own to free space — no automatic early purge of trash, ever. Humans are alerted; humans act.

## Backups & restore

The backup story is deliberately still open — scope, schedule, and mechanics are **Q13** and must be resolved before v1 ships. What is already fixed:

- **Mandatory scope** (exists nowhere else): PostgreSQL (manual tags/confirmations/rejections, users, permissions, shares, event log) and the `versions/` area — the **only copy** of superseded originals ([03](03-storage-and-portability.md#versioning-vs-the-folder-is-everything-known-tension)).
- **Deliberate decision needed**: the derived store is regenerable, but re-deriving 10 TB on CPU-only hardware costs weeks — backing it up may be cheaper than recomputing.
- **Explicitly the user's job**: the source tree is the user's own data; backing it up is their responsibility — the app documents this loudly rather than assuming it silently.
- A restore that has never been exercised is not a backup: the procedure ships **documented and tested**.
