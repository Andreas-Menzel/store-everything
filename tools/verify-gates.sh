#!/usr/bin/env bash
#
# Red-test the pipeline: every gate must FAIL on a deliberately violating sample.
#
# A gate that has never been seen to go red is a claim, not a check
# (11-engineering-standards.md § testing). This script feeds each gate something it is
# supposed to reject and fails if the gate accepts it. It is run in CI and can be run by
# hand at any time; every fixture it creates is removed again.

set -uo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$PWD"

UV="uv --directory server"
PNPM="${PNPM:-pnpm}"

# Locally a missing tool may be skipped: nobody needs every scanner installed to check
# the other gates. In CI it must not be — a gate that can quietly vanish is exactly what
# this script exists to prevent — so the pipeline sets REQUIRE_ALL_GATES=1.
REQUIRE_ALL_GATES="${REQUIRE_ALL_GATES:-0}"

failures=0
skipped=0
fixtures=()

# Restored from a copy taken here, never from git: `git checkout -- openapi.json` would
# throw away uncommitted work on a file this script deliberately mutates.
OPENAPI_BACKUP="$(mktemp -t gate-openapi-XXXXXX.json)"
cp "$REPO_ROOT/openapi.json" "$OPENAPI_BACKUP"

cleanup() {
  for fixture in "${fixtures[@]:-}"; do
    [ -n "$fixture" ] && rm -rf "$fixture"
  done
  [ -f "$OPENAPI_BACKUP" ] && cp "$OPENAPI_BACKUP" "$REPO_ROOT/openapi.json"
  rm -f "$OPENAPI_BACKUP"
}
trap cleanup EXIT

track() { fixtures+=("$1"); }

# Runs a gate that is expected to reject the fixture.
expect_failure() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[31mFAIL\033[0m  %s — the gate accepted a violating sample\n' "$name"
    failures=$((failures + 1))
  else
    printf '  \033[32mok\033[0m    %s\n' "$name"
  fi
}

require_tool() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  fi
  if [ "$REQUIRE_ALL_GATES" = "1" ]; then
    printf '  \033[31mFAIL\033[0m  %s — %s is not installed, and skipping is not allowed here\n' \
      "$2" "$1"
    failures=$((failures + 1))
  else
    printf '  \033[33mskip\033[0m  %s — %s is not installed here\n' "$2" "$1"
    skipped=$((skipped + 1))
  fi
  return 1
}

echo "Red-testing the pipeline gates:"

# --------------------------------------------------------------- python: lint
fixture="server/src/store_everything/_gate_lint.py"
track "$fixture"
printf 'import os\n' > "$fixture"   # imported, never used
expect_failure "ruff lint" $UV run ruff check src/store_everything/_gate_lint.py
rm -f "$fixture"

# ------------------------------------------------------------- python: format
fixture="server/src/store_everything/_gate_format.py"
track "$fixture"
printf 'x = {  "a":1,   "b":2 }\n' > "$fixture"
expect_failure "ruff format" $UV run ruff format --check src/store_everything/_gate_format.py
rm -f "$fixture"

# -------------------------------------------------------------- python: types
fixture="server/src/store_everything/_gate_types.py"
track "$fixture"
printf 'def add(a: int, b: int) -> int:\n    return a + b\n\n\nadd("not", "ints")\n' > "$fixture"
expect_failure "pyright" $UV run pyright src/store_everything/_gate_types.py
rm -f "$fixture"

# ------------------------------------------------------------ contract: drift
# Uses the project's own interpreter: a system `python3` may be anything at all.
$UV run python -c '
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
document = json.loads(path.read_text())
document["info"]["title"] = "Drifted"
path.write_text(json.dumps(document, indent=2) + "\n")
' "$REPO_ROOT/openapi.json"
expect_failure "openapi drift" $UV run pytest tests/test_openapi_document.py -q
cp "$OPENAPI_BACKUP" "$REPO_ROOT/openapi.json"

# ------------------------------------------------------------ licences policy
fixture="$(mktemp -t gate-policy-XXXXXX.json)"
track "$fixture"
printf '{"allowed": ["Nothing-Is-Allowed-1.0"]}\n' > "$fixture"
expect_failure "licence policy" $UV run python -m tools.check_licenses --policy "$fixture" --pnpm "$PNPM"
rm -f "$fixture"

# -------------------------------------------------------- web: shared-layer rules
fixture="web/src/features/_gate/GateProbe.vue"
track "web/src/features/_gate"
mkdir -p "$(dirname "$fixture")"
cat > "$fixture" <<'VUE'
<script setup lang="ts">
// Both of these are forbidden outside the shared layer.
const load = () => fetch('/api/v1/files');
</script>

<template>
  <button @click="load()">go</button>
</template>
VUE
expect_failure "eslint shared-layer rules" $PNPM --filter @store-everything/web exec eslint src/features/_gate/GateProbe.vue
rm -rf "web/src/features/_gate"

# ------------------------------------------------------------------ web: format
fixture="web/src/_gate-format.ts"
track "$fixture"
printf 'export const x   =    {a:1,     b:2}\n' > "$fixture"
expect_failure "prettier" $PNPM exec prettier --check web/src/_gate-format.ts
rm -f "$fixture"

# ------------------------------------------------------------------- web: types
fixture="web/src/_gate-types.ts"
track "$fixture"
printf 'export const answer: number = "forty-two";\n' > "$fixture"
expect_failure "vue-tsc" $PNPM --filter @store-everything/web run typecheck
rm -f "$fixture"

# ------------------------------------------------------------------- spec lint
# Malformed ids break traceability silently, so the lint treats them as errors.
fixture="features/F-999-gate-probe.md"
track "$fixture"
cat > "$fixture" <<'PROBE_MD'
# F-999 — Gate Probe

**Status:** Draft
**Priority:** P2
**Clients:** all
**Depends on:** —

## Functional requirements

- **FR-1** One.
- **FR-1** One again, with the same id.
PROBE_MD
expect_failure "spec lint" $UV run python -m tools.spec_lint
rm -f "$fixture"

# ------------------------------------------------------- traceability matrix gate
# A test marking a requirement that does not exist guards nothing at all.
fixture="server/tests/test_gate_probe.py"
track "$fixture"
report="$(mktemp -t gate-fr-XXXXXX.json)"
track "$report"
cat > "$fixture" <<'PROBE_PY'
import pytest


@pytest.mark.fr("F-999/FR-404")
def test_marks_a_requirement_that_does_not_exist() -> None:
    assert True
PROBE_PY
$UV run pytest tests/test_gate_probe.py -q --fr-report="$report" >/dev/null 2>&1
expect_failure "traceability matrix" $UV run python -m tools.traceability --report "$report"
rm -f "$fixture" "$report"

# ------------------------------------------- traceability: a layer that did not report
# The matrix is merged from one report per test layer (Q59). A layer whose report is absent
# would otherwise read as "nothing there verifies this" — the one wrong answer the matrix
# must never give — so the tool has to refuse rather than proceed.
report="$(mktemp -t gate-fr-XXXXXX.json)"
track "$report"
printf '{"layer": "core", "tests": {}}\n' > "$report"
expect_failure "traceability: absent layer report" \
  $UV run python -m tools.traceability --report "$report" --report /nonexistent/web.json
rm -f "$report"

# ------------------------------------------------- traceability: an untagged web suite
# Vitest refuses a tag the configuration does not declare, and the declarations are read from
# the feature files. A tag naming an FR that does not exist therefore fails at collection —
# the backward gate, arriving before the matrix would have raised it.
fixture="web/src/__gate-probe.spec.ts"
track "$fixture"
cat > "$fixture" <<'PROBE_TS'
import { expect, it } from 'vitest';

it('marks a requirement that does not exist', { tags: ['@F-999/FR-404'] }, () => {
  expect(true).toBe(true);
});
PROBE_TS
expect_failure "vitest requirement tags" \
  $PNPM --filter @store-everything/web exec vitest run src/__gate-probe.spec.ts
rm -f "$fixture"

# ----------------------------------------------------------------- corpus manifest
# A fixture nobody documented is a fixture nobody can trust.
fixture="corpus/fixtures/text/_gate-undocumented.txt"
track "$fixture"
printf 'undocumented fixture\n' > "$fixture"
expect_failure "corpus manifest" $UV run python -m tools.corpus
rm -f "$fixture"

# --------------------------------------------------------------- commit format
if require_tool uvx "commit format"; then
  expect_failure "commit format" uvx --from commitizen cz check --message "broke the convention"
fi

# ------------------------------------------------------------------ secret scan
if require_tool gitleaks "secret scan"; then
  fixture="$(mktemp -d -t gate-secret-XXXXXX)"
  track "$fixture"
  # The body is generated, never written down: a literal secret in this script would be
  # a finding in its own right, and low-entropy filler is ignored by the private-key rule.
  body="$(LC_ALL=C tr -dc 'A-Za-z0-9+/' < /dev/urandom | head -c 64)"
  printf -- '-----BEGIN RSA PRIVATE KEY-----\n%s\n-----END RSA PRIVATE KEY-----\n' \
    "$body" > "$fixture/leaked.pem"
  expect_failure "secret scan" gitleaks dir "$fixture" --no-banner
  rm -rf "$fixture"
fi

echo
if [ "$failures" -gt 0 ]; then
  echo "$failures gate(s) did not reject their violating sample."
  exit 1
fi

if [ "$skipped" -gt 0 ]; then
  echo "All runnable gates rejected their violating sample ($skipped skipped: tool not installed)."
else
  echo "All gates rejected their violating sample."
fi
