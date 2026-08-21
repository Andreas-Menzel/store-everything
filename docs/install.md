# Installing Store Everything

Everything runs as one Docker Compose deployment on a single server, behind a reverse
proxy you already run ([ADR-0005](../decisions/ADR-0005-single-server-docker-network.md),
[ADR-0009](../decisions/ADR-0009-external-traefik-edge.md)).

> **Phase 1, in progress.** Accounts exist: you can log in, manage users, and issue access
> tokens, and the background worker runs. Files and search do not exist yet
> ([ROADMAP](../ROADMAP.md)). Installing now is useful for verifying your proxy, your
> backups and your first admin account — not for storing anything.

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
not be routed traffic. The `orchestrator` waits for the same reason, logging one line —
`waiting before claiming work` — rather than failing; it starts on its own once the schema
is in place, with no restart needed.

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
code. Every `/api/v1/…` path answers `401` except the login endpoint.

**5. Create the first administrator.**

An instance with no accounts cannot create one through the API — only administrators may,
and there is no self-registration. Break the circle once, either way:

```bash
docker compose exec api store-everything create-admin you@example.com
```

The command prompts for the password twice and never echoes it. It refuses once any
account exists, so it is not a way in later.

The unattended alternative is `SE_BOOTSTRAP_ADMIN_EMAIL` and
`SE_BOOTSTRAP_ADMIN_PASSWORD` in `.env`: the account is created at start-up, the event is
audited, and the variables are ignored from then on. Remove the password afterwards — a
secret that no longer does anything is still a secret sitting in a file.

**6. Log in.**

```bash
curl -si https://$PUBLIC_HOST/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"..."}'
```

The response carries a `__Host-se_session` cookie — `HttpOnly`, `Secure`, `SameSite=Lax`.
Browsers keep it; scripts should not. For anything programmatic, log in once and create a
personal access token (`POST /api/v1/auth/tokens`, scope `read` unless it must write): it
travels in `Authorization: Bearer …`, is shown exactly once, and can be revoked on its own.

Ten failed logins for one address, or from one client address, within fifteen minutes stop
further attempts for the rest of that window. The refusal is recorded, and it clears by
itself.

## Upgrading

```bash
docker compose pull && docker compose up -d && docker compose run --rm migrations
```

An upgrade that adds a table follows the same order: the API answers **not ready** and the
orchestrator waits until you have run the migration step. Both then continue by themselves.

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
| `orchestrator` | Runs background work (`store-everything worker`). No ingress, no proxy label — nothing routes to a worker. |
| `migrations` | Runs on request (`docker compose run --rm migrations`) and exits. |

Stopping the orchestrator at any moment is safe, including `kill -9`: work is claimed
under a lease, and an expired lease is picked up by whichever worker starts next
([ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md)). It is also fine to run
none — the API keeps serving and queued work simply waits.

The extractor containers join this file in phase 2, when there is something to run them on.

## Logs

```bash
docker compose logs -f api
```

One JSON object per line on stdout, each carrying the request id that a client-visible
error reports as `instance` — that id is the only bridge between the two, by design.
Logs never contain secrets, tokens, file contents or search queries.
