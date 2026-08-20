# ADR-0015 — Ground-truth corpus: synthetic-first, curated for realism, manifest-governed

**Status:** Accepted
**Date:** 2026-08-20

## Context

[11 § test infrastructure](../specs/11-engineering-standards.md#test-infrastructure) requires one versioned fixture set with a machine-readable manifest of each fixture's truth — PDFs with known phrases on known pages, images with known objects, audio/video with utterances at known timestamps, plus an adversarial set. That single corpus feeds three consumers: FR tests, the golden-query benchmark ([Q8](../OPEN-QUESTIONS.md)), and the extractor conformance kit. [Q28](../OPEN-QUESTIONS.md) asked how it is sourced, licensed, stored, and bounded.

Two constraints sharpen the answer. The repository is public under AGPL-3.0 ([ADR-0016](ADR-0016-license-and-third-party-compliance.md)), so every committed byte must be redistributable and attributable. And the corpus must support *exactness* claims — "page 3, character offset 41" — which curated real-world files rarely come with.

A survey of how comparable projects solve this found a consistent pattern: a small canonical set committed in-repo (Paperless-ngx, Apache Tika, whisper.cpp), a separate assets repository consumed as a submodule (Immich, tesseract), or hash-verified download-on-demand (PhotoPrism, pdf.js's `.link` pointers for non-redistributable files). Nobody in this space uses Git LFS, whose CI bandwidth billing makes it actively expensive for fixtures fetched on every run.

## Decision

**Fixtures are generated where truth must be exact, curated where realism matters, and always governed by a manifest.**

### Sourcing, per fixture class

| Class | Approach | Concrete sources |
|---|---|---|
| Born-digital documents | **Generate** (exact text and positions by construction), plus a few curated files for producer variety | reportlab / LibreOffice headless; openpreserve `format-corpus` (CC0), Govdocs1 (freely redistributable) |
| Scanned documents / OCR | **Generate**: render a synthetic PDF, then degrade with seeded Augraphy passes — known text, reproducible noise; curate a small real set | SROIE (CC-BY-4.0), NIST SD (US-gov PD), Library of Congress free-to-use (PD) |
| Images with objects / EXIF / GPS | **Generate metadata** (exiftool injection = exact expected values) onto CC0 bases; curate for real camera maker-note variety | Wikimedia Commons CC0/PD originals, `exif-samples` (CC-BY-SA), license-filtered COCO images |
| Audio / video with transcripts | **Curate** the honest signal, generate for breadth | LibriSpeech (CC-BY-4.0), Common Voice (CC0), Blender open movies (CC-BY-3.0), NASA (PD); Piper TTS for language/duration coverage |
| Faces *(only if [F-018](../features/F-018-people.md) is pulled in)* | **Synthetic or consented only** | SFHQ (MIT, synthetic). Never scraped face datasets |
| Adversarial | **Generate in-test** where trivial; commit the cleanly licensed collections | zip-slip entries, oversized dimensions, mislabeled extensions, zero-byte and truncated files — generated; PngSuite (permissive) and `format-corpus` error-PDF sets — committed |

**Never committed, never CI-downloaded:** FUNSD, RVL-CDIP/IIT-CDIP, Unsplash imagery, FFHQ, and the scraped face datasets (LFW, VGGFace2, MS-Celeb, MegaFace) — research-only, non-commercial, or consent-less. A fixture whose license cannot be named does not enter the corpus.

### Storage and growth

- **Corpus v0 lives in-repo**, budget **≤ 20 MB total with no single file over 5 MB**, holding one fixture per assertion type.
- **Every fixture has a manifest row**: relative path, SHA-256, source URL, author, license, retrieval date, and the ground truth it asserts. The manifest is the corpus's schema and the input to test parametrization.
- **`corpus/ATTRIBUTION.md` is generated from the manifest and committed**, drift-checked in CI exactly like the OpenAPI document — CC-BY compliance requires attribution to travel with the files.
- **Generators are committed alongside their outputs.** Rendering is not bit-reproducible across tool versions, so the artifact is the fixture and the generator plus its recorded tool version is the provenance.
- **When the corpus outgrows the budget** (bulk media arrives with phases 2–3), it moves to a separate `test-assets` repository consumed as a submodule, or to manifest-driven download-on-demand with SHA-256 verification and CI caching. Non-redistributable references may then be *pointed at* by URL and hash without being mirrored. **Git LFS is excluded.**

## Consequences

- **Exactness claims are cheap and honest**: positional assertions rest on files whose truth we constructed, not on a third party's OCR output treated as gospel.
- **Realism is deliberately rationed** in v0. Producer-quirk coverage grows as phases 2 and 3 add their capabilities — the corpus is a cross-phase track ([ROADMAP](../ROADMAP.md#cross-phase-tracks)), not a phase-0 deliverable that is ever "finished".
- **CI stays fast** because the committed set is small and the heavy set is fetched and cached, not cloned on every run.
- **Licensing is auditable by machine**: a fixture without a complete manifest row fails CI, so the public repository cannot silently acquire an unattributable file.
- **Biometric fixtures carry an extra bar** (synthetic or consented, recorded as such), which is a precondition [Q50](../OPEN-QUESTIONS.md)/[Q51](../OPEN-QUESTIONS.md) inherit if [F-018](../features/F-018-people.md) is pulled forward.
- Resolves [Q28](../OPEN-QUESTIONS.md) for sourcing, licensing, storage, and budget; per-capability fixture *content* keeps arriving with each phase.
