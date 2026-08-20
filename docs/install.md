# Installing Store Everything

Everything runs as one Docker Compose deployment on a single server, behind a reverse
proxy you already run ([ADR-0005](../decisions/ADR-0005-single-server-docker-network.md),
[ADR-0009](../decisions/ADR-0009-external-traefik-edge.md)).

> **Phase 0.** The instance starts, answers its health probes, and refuses every API call
> for want of an identity provider. Accounts, files and search arrive in phase 1
> ([ROADMAP](../ROADMAP.md)). Installing now is useful for verifying your proxy and
> backup arrangements, not for storing anything.

## Requirements

- Docker with Compose v2
- A running Traefik with a shared Docker network, terminating TLS
- A DNS name pointing at that proxy

## First run

**1. Configure.** Copy the example and edit it; `.env` is git-ignored and holds secrets.

```bash
cp .env.example .env
```

At minimum set `PUBLIC_HOST`, `POSTGRES_PASSWORD`, and the matching password inside
`SE_DATABASE_URL`. Set `TRAEFIK_NETWORK` to your proxy's shared network if it is not
called `traefik`.

Set `SE_FORWARDED_ALLOW_IPS` to the proxy's address on that network. Leaving it empty is
safe — the app then believes no `X-Forwarded-*` header at all — but until it is set, the
client addresses in rate limits and audit records will be the proxy's, not the caller's.
Never use a wildcard: a spoofed client IP poisons both.

**2. Start.**

```bash
docker compose up -d
```

The API reports **not ready** at this point, and its container is marked unhealthy. That
is correct: the schema has not been created yet, and an instance that cannot serve should
not be routed traffic.

**3. Apply the schema.**

```bash
docker compose run --rm migrations
```

Migrations are a deliberate step rather than something that happens on every start —
whether they should run automatically on upgrade is still open
([Q20](../OPEN-QUESTIONS.md)). Within a few seconds the API turns healthy and Traefik
begins routing to it.

**4. Check.**

```bash
curl -s https://$PUBLIC_HOST/readyz
```

`{"status":"ready"}` means the database is reachable and the schema matches the running
code. Every `/api/v1/…` path answers `401` until phase 1 adds accounts.

## Upgrading

```bash
docker compose pull && docker compose up -d && docker compose run --rm migrations
```

Migrations are expand–contract, so the previous image keeps working against the migrated
schema; rolling back is redeploying the previous tag. Take a database backup first — the
backup story itself is still open ([Q13](../OPEN-QUESTIONS.md)), and an untested restore
is not a backup.

## Development, without a proxy

```bash
make up            # builds, then publishes 127.0.0.1:8000 and 127.0.0.1:5432
make compose-migrate
make down
```

The override turns the proxy network into an ordinary local one and publishes ports on
the loopback interface, so no Traefik is needed. The Traefik labels stay in place and are
simply inert.

## What the stack contains

| Service | Exposure |
|---|---|
| `api` | The only service on the proxy network. Serves plain HTTP internally; TLS is the proxy's job. |
| `postgres` | Internal only, no published port. Data lives in the `postgres-data` volume. |
| `migrations` | Runs on request (`docker compose run --rm migrations`) and exits. |

The orchestrator and the extractor containers join this file in phases 1 and 2, when
there is something for them to run.

## Logs

```bash
docker compose logs -f api
```

One JSON object per line on stdout, each carrying the request id that a client-visible
error reports as `instance` — that id is the only bridge between the two, by design.
Logs never contain secrets, tokens, file contents or search queries.
