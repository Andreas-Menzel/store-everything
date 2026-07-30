# ADR-0002 — Extractors as Docker containers behind a fixed, pull-based API

**Status:** Accepted
**Date:** 2026-07-29

## Context

Content analysis (OCR, text extraction, transcription, object/scene detection, previews, embeddings) must be extensible: new file types and better models will arrive for years. Files are large (multi-GB video within 10 TB total); analysis on CPU-only hardware runs minutes to hours. The system is security-first: analysis code handles all user content.

## Decision

We will implement every analysis capability as a **separate Docker container ("extractor") speaking one fixed, versioned API** ([spec 05](../specs/05-extractor-contract.md)):

- **Registration with capability manifest** (accepted MIME types/derived kinds, produced output kinds, extractor + model version, cost class, GPU/network needs). The core routes files to matching extractors — no broadcast, no core changes to add a capability.
- **Pull-based file access**: the core hands a read-only file *reference*; the extractor reads bytes/ranges itself. No pushing multi-GB payloads to N extractors.
- **Async job lifecycle**: accept → progress → result, idempotency keys, retries, timeouts, dead-letter. Extractor downtime degrades to missing facets, never failed ingestion.
- **Structured result envelope** (metadata, text segments with anchors, tags with confidence, embeddings, derived assets), stamped with extractor/model/version/generation provenance.
- **No outbound network by default**; remote-AI extractors are explicitly network-enabled configuration.

## Consequences

- Adding a capability = adding a container; the plugin boundary is honest (only the fixed API is shared).
- Selective reprocessing falls out of the manifest (new image model → rerun `image-vision` over images only).
- The contract is location-agnostic even though v1 co-locates everything (ADR-0005) — remote extractors later are a resolution change for file references, not a redesign.
- We own contract versioning discipline: the API can only grow compatibly within v1.
- Sandboxing/enforcement details (read-only mounts, network policy) still to be specified (Q7).
