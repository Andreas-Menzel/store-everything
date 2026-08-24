# Store Everything

A self-hosted personal cloud for a small group of people (1–30) that stores files of every type — and makes them *findable*. Think Google Drive or pCloud on your own hardware, but with search as the actual product:

- **Exact search** — "I know a phrase, name, or date in the file" returns the file *and the position*: document pages 1, 3 and 7; video at 04:12.
- **Semantic search** — "photo of my dog at the beach" finds an image whose detected content is *dog, sand, ocean*, without sharing a single word with the query.

To make that possible, every uploaded file is analyzed by **pluggable extractors** (OCR, text extraction, transcription, object detection, preview generation) that run as local Docker containers speaking one fixed API. Adding support for a new file type means adding a container, not changing the core.

## How it's built (the principles)

- **Local-first, security-first.** A default deployment runs everything — including AI inference — on the server it's installed on and makes zero external network calls. Remote AI models are always explicit opt-in.
- **API-first.** The HTTP API is the only real interface; web UI, CLI, and future apps or AI agents are just API consumers.
- **The app is removable at any time.** Files live as plain files in the user's own folder hierarchy. Everything the app adds — index, tags, embeddings, previews, transcripts — is a derived, regenerable layer. Delete the app and you keep all your data.
- **Originals are never modified.** Derived data is stored separately and stamped with its provenance, so it can be replaced when better models arrive while manual work survives.
- **Multi-user from the start.** Accounts, workspaces, permissions, and sharing — with permission-aware search, because a result snippet is a data leak if you can't open the file.

## The stack

PostgreSQL with pgvector as the single datastore ([ADR-0001](decisions/ADR-0001-postgresql-single-datastore.md)) · Python 3.13 + FastAPI core, SQLAlchemy Core and hand-written SQL ([ADR-0012](decisions/ADR-0012-python-fastapi-core-stack.md)) · an owned crash-only operation layer instead of a job-queue library ([ADR-0013](decisions/ADR-0013-owned-operation-layer.md)) · a Vue 3 SPA talking to a generated OpenAPI client ([ADR-0014](decisions/ADR-0014-vue-frontend-stack.md)) · Docker Compose behind an existing Traefik ([ADR-0005](decisions/ADR-0005-single-server-docker-network.md), [ADR-0009](decisions/ADR-0009-external-traefik-edge.md)).

## Status

**Phase 2 complete (v0.3.0); phase 3 — search and the library — is next.** Files are now *analysed*. Pluggable extractor containers claim work by polling ([ADR-0020](decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md)) from a network with no route out ([ADR-0021](decisions/ADR-0021-extractor-sandbox-enforcement.md)), and six of them ship: thumbnails at a fixed tier set with a placeholder that paints before the image arrives, PDF pages rendered when somebody asks, EXIF and document metadata as typed facts, a document's own text page by page, and OCR on the pages that have none — routed there because the text extractor wrote `needs_ocr` and the OCR extractor's manifest binds to that key, with neither of them naming the other. Tags carry provenance through it all ([ADR-0004](decisions/ADR-0004-tag-provenance-and-reprocessing.md)): a machine proposes, an admin admits the word to the vocabulary, a person confirms — and a confirmation survives every re-run while a rejection is never re-added.

**Nothing is *searchable* yet.** Segments exist with their positions (page 3, lines 7–12) and are readable per file, but the query side — full-text, embeddings, ranking, facets, the timeline — is phase 3. Files are still found by name, path and tag, not by content.

This repository holds the project's specifications (`specs/`), user-facing feature definitions (`features/`), architecture decision records (`decisions/`), open questions (`OPEN-QUESTIONS.md`), the implementation roadmap ([`ROADMAP.md`](ROADMAP.md)) — the phase-by-phase order in which the features will be built — the core service in [`server/`](server/README.md), the web UI in [`web/`](web/README.md), and the API contract in `openapi.json`, from which the typed client in `packages/api-client` is generated.

`make help` lists every task; `make check` runs everything the pipeline will. Installing an instance: [docs/install.md](docs/install.md).

## License

[AGPL-3.0-only](LICENSE). Third-party dependencies, test fixtures, and model weights keep their own licenses; compliance is a CI gate, and attribution is generated rather than hand-maintained ([ADR-0016](decisions/ADR-0016-license-and-third-party-compliance.md)).
