# ADR-0001 — PostgreSQL as the single datastore

**Status:** Accepted
**Date:** 2026-07-29 *(accepted 2026-08-20, phase-0 stack gate)*

## Context

Search requires: exact phrase matching, file-name/substring search, typed metadata filters (dates, numbers, geo), relevance-ranked full-text search, vector ANN search over multiple embedding spaces, facets, snippets — and **permission filtering inside every query**. The system also needs a job queue for the ingestion pipeline. Hardware is a self-hosted, possibly CPU-only server; operators are individuals, so every additional stateful service multiplies install, backup, and failure-mode complexity. Scale target: ~10 TB source data, a few million files/segments/vectors, 10–30 users. Running both a dedicated search engine (Elasticsearch/Meilisearch/…) and a vector DB alongside a relational DB means three systems that can disagree — permission changes racing index updates is exactly the class of bug a security-first product can't afford.

## Decision

We will use **PostgreSQL as the single datastore**: relational domain model and permissions, full-text search (`tsvector`), trigram indexes for name search, **pgvector** (HNSW) for embeddings, typed metadata tables, and the ingestion job queue (`FOR UPDATE SKIP LOCKED`). All search runs as queries that join the permission model transactionally.

We will hide search behind an internal interface so a dedicated keyword/vector engine can be swapped in later if PostgreSQL's ranking quality or performance ceiling is actually hit.

## Consequences

- One dependency to install, back up, monitor; one consistency domain — permissions and index can never diverge.
- Job queue state participates in the same transactions as domain state (no broker).
- We accept that BM25-grade ranking and advanced FTS features are weaker than dedicated engines; RRF fusion and tuning must compensate. If they can't, the escape hatch is the search interface, at the cost of a second system and an index-sync mechanism.
- pgvector HNSW at a few million vectors is well within known-good territory; we must keep query-side embedding models resident in memory to hit search latency targets (that's app design, not DB).
