/**
 * The requirement ids that exist, read from the feature files.
 *
 * Vitest requires every tag a test uses to be declared in the configuration. A hand-written list
 * of 350 ids would be stale within a week — the staleness bug the matrix exists to prevent — so
 * the list is derived from the same documents the matrix is built from: add an FR, and the tag
 * for it exists. A tag for an id that does *not* exist fails the suite at collection, which is
 * the backward gate arriving earlier than the matrix would have raised it.
 *
 * This reads structure, not meaning: the ids only. `server/tools/specdocs.py` remains the one
 * parser for everything else about a feature file, and the two patterns below are kept identical
 * to its `_REQUIREMENT` and `_INVARIANT`.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { METHOD_TAGS, REPO_ROOT } from './report.ts';

const FEATURES_DIR = join(REPO_ROOT, 'features');
const INVARIANTS_DOC = join(REPO_ROOT, 'specs', '02-domain-model.md');

const FEATURE_FILE = /^F-(\d{3})-[a-z0-9-]+\.md$/;
const REQUIREMENT = /^-\s+\*\*FR-(\d+)\*\*/;
const INVARIANT = /^(\d+)\.\s+\S/;

export function requirementIds(): string[] {
  const ids: string[] = [];

  for (const name of readdirSync(FEATURES_DIR).sort()) {
    const feature = FEATURE_FILE.exec(name);
    if (feature === null) continue;
    for (const line of readFileSync(join(FEATURES_DIR, name), 'utf8').split('\n')) {
      const requirement = REQUIREMENT.exec(line);
      if (requirement !== null) ids.push(`F-${feature[1]}/FR-${requirement[1]}`);
    }
  }

  // The invariants are the numbered list under `## Invariants`, and nothing else in the document
  // is a numbered list — the same assumption `specdocs.py` makes.
  const invariants = readFileSync(INVARIANTS_DOC, 'utf8').split('## Invariants')[1] ?? '';
  for (const line of invariants.split('\n')) {
    const invariant = INVARIANT.exec(line);
    if (invariant !== null) ids.push(`02/INV-${invariant[1]}`);
  }

  return ids;
}

/** Every tag the web suites may use: one per requirement, plus the non-default methods. */
export function requirementTags(): { name: string }[] {
  return [...requirementIds().map((id) => `@${id}`), ...Object.keys(METHOD_TAGS)].map((name) => ({
    name,
  }));
}
