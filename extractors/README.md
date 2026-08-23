# Extractors

An **extractor** is a container that adds one analysis capability to a Store Everything instance:
document text, OCR, thumbnails, transcription, whatever comes next. It speaks one fixed API and
nothing else — that boundary is what lets a capability be added without touching the core
([ADR-0002](../decisions/ADR-0002-extractor-containers-fixed-api.md),
[spec 05](../specs/05-extractor-contract.md)).

This directory holds three things:

| Path | What it is |
|---|---|
| `src/se_extractor/{client,loop,models}.py` | The **SDK**: the six calls, and the worker loop around them |
| `src/se_extractor/reference.py` | The **reference extractor** — an executable example, a test double, and the image the conformance kit validates itself against |
| `src/se_extractor/conformance.py` | The **conformance kit**: does this image speak the contract, and does this core? |

## The contract is the boundary, not this SDK

`openapi-extractor.json` at the repository root describes the whole contract. An extractor that
implements those calls needs nothing from this package, in any language. The SDK exists because
the calls have a shape worth getting right once — a lease that has to be kept while blocking work
runs, a fencing token that has to travel with every write, a core that may not be up yet when the
container starts.

The SDK is licensed like the rest of the repository (**AGPL-3.0-only**), so importing it puts an
extractor under those terms. Implementing the calls directly does not; that is a deliberate
consequence of where the boundary sits, and the reason the contract is published as a document
rather than only as code. Whether an official permissively-licensed client should exist is
[Q61](../OPEN-QUESTIONS.md).

## Writing one

```python
from se_extractor import ExtractorClient, PermanentFailure, run

MANIFEST = {
    "id": "my-extractor",              # provisioned by an administrator before you start
    "version": "1.0.0",                # a bump makes files eligible for reprocessing
    "api_version": "v1",
    "model": {"name": "some-model", "version": "3.1"},   # optional; it is provenance
    "accepts": {"mime_types": ["image/*"]},
    "produces": ["metadata"],
    "cost_class": "medium",            # light | medium | heavy — a scheduler hint
    "network": "none",                 # `outbound` is an explicit, admin-visible opt-in
}


def handle(job, context):
    with context.client.stream_input(job) as chunks:
        for chunk in chunks:
            context.raise_if_cancelled()   # check it in any loop that can run for a while
            ...
    return None                            # or a result envelope


run(ExtractorClient("http://api:8000", token), MANIFEST, handle)
```

What the loop does for you: registers (waiting patiently while the instance starts), claims with a
long poll, heartbeats in a thread while your handler blocks, turns an unhandled exception into a
retryable failure and a `PermanentFailure` into one that skips the retries, and drops the work
quietly if the lease was lost while you held it.

Rules worth knowing before you write the handler:

- **Check the hash.** The job declares the content hash of every input. Verifying it is one line
  and it is how you find out you analysed the wrong bytes — `reference.py` shows it.
- **Never write outside your job.** Your only filesystem access is the job's own input URLs. There
  is no mount, and there is no route from your container to anything but the extractor API
  ([ADR-0021](../decisions/ADR-0021-extractor-sandbox-enforcement.md)).
- **Being killed is normal.** Nothing is lost: your lease lapses and the job is claimed again.
  Do not try to be clever about shutdown.
- **Do not retry a bad input forever.** If the file will never work, raise `PermanentFailure`.

## Running the reference extractor

```bash
SE_CORE_URL=http://localhost:8000 \
SE_EXTRACTOR_TOKEN=seext_… \
uv run --directory extractors se-reference-extractor
```

`SE_REFERENCE_MODE` picks its behaviour: `verify` (default — read the input and check it against
the declared hash), `succeed`, `fail`, `fail-permanently`, `stall` (never finish, so the lease
lapses and the re-run path can be exercised). `SE_REFERENCE_DELAY_SECONDS` slows it down.

## Shipping one

An extractor is a container on the `extractors` network, and that network has no gateway — it can
reach the API and nothing else. `compose.extractor-example.yaml` at the repository root is a
working service block with the whole hardening baseline: a read-only root filesystem, a `tmpfs`
for scratch, no capabilities, an unprivileged user, bounded memory, CPU and processes, and no
published port (nothing listens — dispatch is poll-based). Copy it and change the image, the
token variable and the resource ceilings.

Two properties your image has to hold up its end of:

- **do not expect a mount.** Inputs arrive over HTTP, one job at a time. There is no bind mount
  of the library, and asking for one is asking for the sandbox to be pointless.
- **run unprivileged, and install nothing at runtime.** The `Dockerfile` here builds its
  virtualenv in a builder stage and removes pip from the runtime; CI asserts both for every
  official image.

`make sandbox` proves the topology from inside a real container, positive control included.

## Running the conformance kit

The kit needs an administrator on a running instance. Point it at one:

```bash
uv run --directory extractors se-conformance \
  --base-url http://localhost:8000 --email admin@example.com --password … \
  --extractor-id reference
```

Without `--extractor-id` it runs only the **protocol** checks, which validate the *core*: a
manifest that is refused for the right reason, an identity that cannot be borrowed, an exclusive
output kind with one owner, a stale fencing token that cannot write, a disabled extractor that is
told so. With one, it also runs the **image** checks against an extractor that is already running:
it registers, its manifest is coherent, it claims work promptly, it finishes, and it ignores what
it never said it accepts.

A check that cannot be run here reports `skip` with the reason. The exit code is the answer.
