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

**7. Create a workspace.**

A workspace is a top-level folder of files, owned by exactly one user. The simple kind is
*managed*: the app creates the directory under `SE_DATA_ROOT`, named after the workspace, and
any member can make one for themselves.

```bash
curl -s https://$PUBLIC_HOST/api/v1/workspaces -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" -d '{"name":"Photos"}'
```

The answer comes back with `"state": "provisioning"`: the request records the workspace, and
the orchestrator creates its directory, plants the `.workspace/` control directory and
registers the root folder. Read the workspace again — it flips to `active` within seconds.
If it does not, the orchestrator is not running or its work is failing; `store-everything
verify` and `docker compose logs orchestrator` say which.

Each workspace root gets one `.workspace/` directory and nothing else
([ADR-0018](../decisions/ADR-0018-workspace-layout-and-adoption.md)). It holds a `marker`
identifying the tree and a `staging/` area where writes land before being renamed into
place — which is why it has to live *inside* the tree rather than on the app volume. To hide
it from Windows and macOS clients browsing the share over Samba, add to the share's
`smb.conf`:

```ini
veto files = /.workspace/
delete veto files = yes
```

**Adopting an existing folder.** To index a tree you already have — a NAS share full of
photos — without copying a byte, mount it into **both** the `api` and `orchestrator`
containers at the same path, allow-list it, and let an administrator create the workspace
over it:

```yaml
# compose.override.yaml
services:
  api:
    volumes: ['/mnt/nas/photos:/mnt/nas/photos']
  orchestrator:
    volumes: ['/mnt/nas/photos:/mnt/nas/photos']
```

```bash
# .env
SE_ADOPTION_ROOTS=/mnt/nas/photos
```

```bash
curl -s https://$PUBLIC_HOST/api/v1/workspaces -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"name":"The NAS","adopt_path":"/mnt/nas/photos","owner":"<user-id>"}'
```

Three things about this are deliberate, and each one refuses rather than guesses:

- **Adoption is admin-only, and the allow-list is empty by default.** A member can never
  submit a filesystem path at all. The blast radius of a mistaken or hostile path is
  whatever you listed, nothing more.
- **The path is the one inside the container.** This is the mistake that costs the most time:
  `/mnt/nas/photos` on the host is not that path in the container unless you mounted it there.
  A path that is not inside `SE_ADOPTION_ROOTS`, is not a directory, overlaps another
  workspace, or resolves through a symlink to somewhere else is refused, naming which.
- **The filesystem is checked, not assumed.** The root is probed for atomic rename and honest
  `fsync` before it is accepted ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md));
  a filesystem that fails is refused naming the property that failed. Run the probe yourself
  first if you would rather know before you ask:

```bash
docker compose exec api store-everything fs-check /mnt/nas/photos
```

v1 supports filesystems local to the app host. An SMB or NFS mount is not supported until the
probe passes against that mount with its own options — on a filesystem that lies about
`fsync`, every durability guarantee this app makes is void, and a refused workspace is better
than silent loss.

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

Three volumes hold state, and they are **not** equally replaceable:

| Volume | Contents | If you lose it |
|---|---|---|
| `postgres-data` | The database: accounts, tags, permissions, the event log | Everything the app knows is gone |
| `workspace-data` | `SE_DATA_ROOT` — the file trees of every *managed* workspace | The users' own files are gone. This is the data |
| `app-data` | `versions/` (superseded file content) and `derived/` (previews) | `versions/` is the **only copy** of overwritten content — back it up. `derived/` rebuilds by reprocessing, at the cost of CPU |

Files in an **adopted** workspace are in neither: they stay in the directory you pointed the
app at, which is yours to back up
([ADR-0003](../decisions/ADR-0003-files-on-disk-source-of-truth.md)). If you would rather keep
managed workspaces on your own storage too, replace the `workspace-data` volume with a bind
mount to that path — the files are plain files in plain directories either way, which is the
whole point ([03](../specs/03-storage-and-portability.md)).

Stopping the orchestrator at any moment is safe, including `kill -9`: work is claimed
under a lease, and an expired lease is picked up by whichever worker starts next
([ADR-0010](../decisions/ADR-0010-crash-only-execution-model.md)). It is also fine to run
none — the API keeps serving and queued work simply waits.

The extractor containers join this file in phase 2, when there is something to run them on.

## Checking an instance

```bash
docker compose exec api store-everything verify
```

Read-only. It reports debris the janitor should have collected, operations stuck without a
worker, and blobs whose content no longer matches their digest — the invariants that relate
rows to bytes, which nothing can enforce at the moment of use. Clean output names the checks
it ran, so a quiet result is not mistaken for a check that did not happen.

```bash
docker compose exec api store-everything fs-check /path/to/a/directory
```

Asks whether a directory can safely hold data: `fsync` on files and directories, rename onto
an existing file, consistent listings, and staging on the same device as its destination. It
also reports whether the filesystem folds case or normalizes Unicode, which decides what
counts as the same filename ([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)).

## Logs

```bash
docker compose logs -f api
```

One JSON object per line on stdout, each carrying the request id that a client-visible
error reports as `instance` — that id is the only bridge between the two, by design.
Logs never contain secrets, tokens, file contents or search queries.
