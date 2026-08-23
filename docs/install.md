# Installing Store Everything

Everything runs as one Docker Compose deployment on a single server, behind a reverse
proxy you already run ([ADR-0005](../decisions/ADR-0005-single-server-docker-network.md),
[ADR-0009](../decisions/ADR-0009-external-traefik-edge.md)).

> **Phase 1, in progress.** Accounts exist: you can log in, manage users, and issue access
> tokens, and the background worker runs. Files and search do not exist yet
> ([ROADMAP](../ROADMAP.md)). Installing now is useful for verifying your proxy, your
> backups and your first admin account — not for storing anything.

## Requirements

- Docker with Compose v2. **Engine 25.0.5 or newer** if you will install extractors — older
  engines leak DNS out of isolated networks (CVE-2024-29018), which is the one thing their
  sandbox cannot survive. 28.x recommended.
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

**Set `SE_FORWARDED_ALLOW_IPS` to the proxy's address on that network.** Leaving it empty is
safe against spoofing — the app then believes no `X-Forwarded-*` header at all — but it is not
free: every caller arrives as the proxy, so audit records name the proxy instead of the caller,
and the app stops counting abuse per address because one address would mean the whole instance
(it says so rather than pretending; the per-account login ceiling still applies). Set it to get
per-caller limits and honest audit records. Never use a wildcard: a spoofed client IP poisons
both.

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

## Uploads and the proxy in front

Uploads speak the [IETF resumable-upload protocol](../decisions/ADR-0017-resumable-upload-protocol.md),
which is the only way bytes get in. Two things about that are the proxy's business, and both
bite silently:

**Raise Traefik's read timeout.** `respondingTimeouts.readTimeout` defaults to **60 seconds and
covers the whole request including its body**, so every upload slower than a minute dies with a
truncated request and no useful error. In your static configuration:

```yaml
entryPoints:
  websecure:
    address: ':443'
    transport:
      respondingTimeouts:
        readTimeout: 0s # or a value that fits your slowest upload
```

**Do not put a body-buffering middleware in front of this.** The app streams an upload to disk
as it arrives and `fsync`s before acknowledging each offset; a buffering proxy turns that into
"hold a multi-gigabyte body in memory, then write it".

**The size limits are yours to set,** and they are published to clients in the `Upload-Limit`
header rather than discovered by failing:

| Setting | Default | What it decides |
|---|---|---|
| `SE_UPLOAD_MAX_APPEND_SIZE` | 64 MiB | The largest body one request may carry. It also caps a *single-request* upload — anything larger must use the resumable flow, which our clients do. Keep it under whatever your proxy will pass. |
| `SE_UPLOAD_MIN_APPEND_SIZE` | 1 MiB | The smallest body an append should carry. Appends at least this large are not counted against `SE_RATE_LIMIT_PER_MINUTE`, so a fast link uploading a large file cannot rate-limit itself. |
| `SE_UPLOAD_MAX_SIZE` | 0 (unlimited) | A ceiling on one file. `0` publishes no ceiling rather than a fictional one. |
| `SE_UPLOAD_EXPIRY_DAYS` | 7 | How long an interrupted upload can be resumed. Its staged bytes sit in the workspace's `.workspace/staging/` until then; the janitor collects them afterwards. |

Uploading one file, end to end:

```bash
curl -s -X POST "https://$PUBLIC_HOST/api/v1/workspaces/$WORKSPACE/files?path=Photos/beach.jpg" \
  -H "Authorization: Bearer $TOKEN" -H 'Upload-Complete: ?1' \
  -H 'Content-Type: image/jpeg' --data-binary @beach.jpg
```

The response is the registered file, and `Location` names it. Add
`?content_hash=<sha256>` and the server verifies the assembled bytes against it before
publishing anything — a mismatch fails the upload rather than storing a corrupt file.

## Importing an existing folder, and keeping up with it

An adopted workspace scans itself as soon as it is provisioned: every file is registered with
its path, size, timestamp and SHA-256, and nothing is moved, renamed or rewritten. After that
each workspace is scanned again on a schedule — hourly by default
(`SE_WORKSPACE_SCAN_INTERVAL_MINUTES`) — because that is the only mechanism that catches a
file someone copied onto the share by hand
([ADR-0019](../decisions/ADR-0019-source-tree-semantics.md)).

Watch an import, or ask for one now:

```bash
curl -s "https://$PUBLIC_HOST/api/v1/workspaces/$WORKSPACE/import-status" \
  -H "Authorization: Bearer $TOKEN"
```

```bash
curl -s -X POST "https://$PUBLIC_HOST/api/v1/workspaces/$WORKSPACE/rescan" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```

A rescan does not start a second traversal: it pulls the workspace's pending scan forward, so
asking twice costs nothing. Pass `{"path": "Photos/2026"}` to scan one subtree.

**The first pass reads every byte** — a file's hash is its version's identity, and it is what
later makes a moved or duplicated file recognisable. On a 10 TB share that takes as long as
reading 10 TB takes; `import-status` reports directories scanned, files registered and how much
of the tree is still queued while it runs. Later passes compare size and modification time and
read only what looks changed. It is safe to restart the stack mid-import: the scan checkpoints
after every directory and resumes where it stopped.

**Two things a scan reports instead of importing**, because resolving either would mean
touching your files:

| Reported as | What it means | What to do |
|---|---|---|
| `conflict` | Two names in one directory that the app cannot tell apart — `Report.pdf` beside `report.pdf`, or the same name written in NFC and NFD. One of them is imported and the rest are listed with both spellings. | Rename one of them on disk (or in the app), then rescan |
| `skipped` | A symbolic link (never followed, at any depth), a name over 255 bytes or containing a control character, something that is not a regular file, or a directory that could not be read | Nothing, unless you expected that entry to be imported |

The distinction between a directory that is **gone** and one that could not be **read** is
deliberate: only the first is a statement about the files inside it. A share that fails to
mount produces skipped entries, never deletions.

### Noticing a change in seconds instead of within the hour

The scheduled scan is the backstop; the **watcher** is what makes a file dropped onto the storage
show up while you are still looking at the folder. It is on by default
(`SE_WATCHER_ENABLED`), watches every workspace root, and turns a burst of filesystem activity —
copying five hundred photos is one burst — into a single scan of the directory that covers it.
`import-status` reports whether a root is actually being watched:

```bash
curl -s "https://$PUBLIC_HOST/api/v1/workspaces/$WORKSPACE/import-status" \
  -H "Authorization: Bearer $TOKEN" | jq .watch
```

Three answers, none of which is an error:

| `state` | What it means |
|---|---|
| `watching` | Events on this root hasten its next scan |
| `unwatched` | No worker holds a subscription — the watcher is off, or the orchestrator is not running |
| `unavailable` | The app tried and could not; `detail` says why, and the scheduled scan still covers the workspace |

The one cause worth acting on is the Linux watch limit: each watched directory costs one inotify
watch, so a large tree can exceed `fs.inotify.max_user_watches`. Raise it (`sysctl -w
fs.inotify.max_user_watches=524288`, persisted in `/etc/sysctl.d/`) or accept the hourly pass.

**Kernel events do not fire for changes made on the server side of an SMB or NFS mount**, so on
such a mount the watcher subscribes and simply never hears anything — the state says `watching`
because the app did subscribe, and the schedule is what actually finds those changes. That is the
reason the schedule exists.

### What a rescan does about files that changed

Every pass after the first reconciles what it finds against what the app already knows, and
`import-status` counts each outcome — `files_changed`, `files_moved`, `files_trashed`,
`files_restored`:

| On the share | In the app |
|---|---|
| A file was edited | A new version. The previous one keeps its extracted data and is marked `restorable: false` — its bytes were overwritten before the app could copy them, and saying so beats promising a restore that would fail |
| A file was moved or its folder renamed | The same file at a new path: same id, same history, nothing re-imported. Recognised by content, and among identical files by name |
| A file was deleted | A trash entry badged *removed outside the app*, kept for 30 days. Never dropped from the index silently |
| A deleted file came back | The original entry is reactivated, with its history |

Overwriting **through** the app is the one case where nothing is ever lost: upload with
`?if_exists=new_version` and the previous content is copied into the app-owned `versions/` area
before the new bytes land, so it stays restorable. Without that parameter an upload to an
occupied path is refused (`409`).

**If a share is not mounted, the app notices rather than reacting.** Every workspace root
carries a `.workspace/marker` file written when the workspace was created. A scan that does not
find it — or finds one belonging to a different workspace — registers nothing, reconciles
nothing, and reports the reason in `import-status`; the next scheduled pass tries again. Without
that check, one pass over an empty mount point would move an entire library into the trash. So:
if scans start failing with a marker complaint, check the mount, not the app. Deleting
`.workspace/` from a root has the same effect, which is why it is the one directory in your tree
that belongs to the app.

## Installing an extractor

An **extractor** is a container that analyses files: document text, OCR, thumbnails,
transcription. Installing one is always the same three steps, and the first official one —
`preview-gen`, which renders the thumbnails and placeholders every grid needs — is already in
`compose.yaml`. It will not start until you have done step 1 for it:

```bash
curl -X POST https://YOUR-HOST/api/v1/extractors \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"id":"preview-gen"}'
curl -X POST https://YOUR-HOST/api/v1/extractors \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"id":"pdf-pages"}'
# → put the credentials in .env as SE_PREVIEW_GEN_TOKEN and SE_PDF_PAGES_TOKEN
# → docker compose up -d
```

Until they run, files are stored, searchable by name and readable — they simply have no
thumbnails and no page images, and the API says so per file rather than leaving a client to
discover it from a broken image.

**1. Provision its id and mint its credential.** As an administrator:

```bash
curl -X POST https://YOUR-HOST/api/v1/extractors \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"id":"pdf-text"}'
```

The response carries the credential **once**. Nothing else will ever show it again, which is the
same rule personal access tokens follow. An extractor cannot register itself into existence: the
credential is bound to the id you chose here, so a leaked one cannot invent a second extractor or
stamp another's provenance.

**2. Put the credential in `.env`** and add the service. `preview-gen` in `compose.yaml` is the
worked example of a real one; `compose.extractor-example.yaml` is the annotated template, running
the reference extractor, which reads each file it is given and checks the bytes against the hash
the API declared:

```bash
docker compose -f compose.yaml -f compose.extractor-example.yaml up -d
```

**3. Check it registered.** `GET /api/v1/extractors` shows what each one declared, when it was
last seen, and whether it is enabled. An extractor that never appears has not reached the API;
its logs will say why.

### What the sandbox guarantees, and what it does not

Extractors live on the `extractors` network, which is declared `internal: true` — **no gateway**.
A container there can reach the one other member, the API, and nothing else: not PostgreSQL, not
Traefik, not the internet. That is enforced by the network's topology rather than by anything
inside the container, which is the only way it could be enforced at all
([ADR-0021](../decisions/ADR-0021-extractor-sandbox-enforcement.md)).

The rest of the baseline, all in the example file: a read-only root filesystem, a `tmpfs` for
scratch, no capabilities, no new privileges, an unprivileged user, and bounded memory, CPU and
process count. An extractor also **has no mount of your files**. Its inputs arrive over HTTP, one
job at a time, so a compromised image can read the bytes of the work it was given and nothing
else.

Two things are worth being plain about:

- **Docker Engine >= 25.0.5 is a requirement, not a suggestion.** Before that release the
  embedded DNS resolver forwarded lookups out of internal networks (CVE-2024-29018) — a
  data-exfiltration channel that no amount of network isolation closes. 28.x is recommended; it
  also stops unsolicited LAN traffic reaching unpublished container ports.
- **A network-enabled extractor is a deliberate exception.** One whose manifest declares
  `network: outbound` — a remote-AI backend — needs an extra network in its service block, and
  the admin extractor list shows the flag. The registry surfaces the intent; the compose file is
  what grants it. Review them together.

You can prove all of this on your own machine, from inside a real container:

```bash
make sandbox
```

It brings up a throwaway stack, asks the extractor what it can reach, and tears the stack down.
The first thing it checks is that the extractor *can* reach the API — a container isolated from
everything would otherwise pass a test for isolation while being useless.

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
| extractors | One service per installed analysis capability, on a network with **no gateway**: it can reach the API and nothing else. None ship yet — see [Installing an extractor](#installing-an-extractor). |

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
