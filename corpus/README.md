# Ground-truth corpus

One fixture set feeding the FR tests, the golden-query benchmark and the extractor
conformance kit ([11 § test infrastructure](../specs/11-engineering-standards.md#test-infrastructure),
[ADR-0015](../decisions/ADR-0015-ground-truth-corpus-strategy.md)).

```bash
make corpus     # regenerate fixtures, refresh the manifest, rewrite ATTRIBUTION.md
```

Every fixture has a row in [`manifest.json`](manifest.json) recording where it came from,
under which licence, and **what truth it asserts**. An unlisted file fails the build: a
fixture nobody can explain is one nobody can trust. `sha256` and `bytes` are filled in by
the tool; the licence and the assertion are written by a human, because both are claims
someone stands behind.

## Adding a fixture

- **Generated** — extend [`generate.py`](generate.py) and add a manifest row naming it.
  Output must be reproducible: no timestamps, no randomness, or `make corpus` will show
  spurious changes on every run.
- **Curated** — copy the file in and add a row with `origin: "curated"` and a `source`
  block (`url`, `author`, `retrieved`). Only redistributable licences; the
  [never-commit list](../decisions/ADR-0015-ground-truth-corpus-strategy.md) is part of
  the decision, not a preference.

Budget: **20 MB total, 5 MB per file.** When the corpus outgrows that — bulk media arrives
with phases 2 and 3 — it moves to a separate `test-assets` repository or to
manifest-driven download-on-demand, both already sketched in ADR-0015.

## What is not here, and why

**Names that collide** under case-folding or unicode normalisation are described in
`fixtures/adversarial/hostile-names.json` rather than committed as files. Both members of
such a pair cannot exist side by side on a case-insensitive volume, so committing them
would break checkout on macOS and Windows. Tests materialise them at runtime and skip
where the filesystem cannot hold them — which is itself the observation
[Q25](../OPEN-QUESTIONS.md) has to resolve.

**Positioned documents, images and audio/video** arrive with the phases that can read
them (2 and 3). Phase 0 ships only fixtures whose truth needs no extractor to verify.
