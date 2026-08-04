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

## Status

**Planning phase — no code yet.** This repository holds the project's specifications (`specs/`), user-facing feature definitions (`features/`), architecture decision records (`decisions/`), open questions (`OPEN-QUESTIONS.md`), and the implementation roadmap ([`ROADMAP.md`](ROADMAP.md)) — the phase-by-phase order in which the features will be built.
