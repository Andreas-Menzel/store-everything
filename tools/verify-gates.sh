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

failures=0
skipped=0
fixtures=()

cleanup() {
  for fixture in "${fixtures[@]:-}"; do
    [ -n "$fixture" ] && rm -rf "$fixture"
  done
  git -C "$REPO_ROOT" checkout --quiet -- openapi.json 2>/dev/null || true
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
  printf '  \033[33mskip\033[0m  %s — %s is not installed here\n' "$2" "$1"
  skipped=$((skipped + 1))
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
git checkout --quiet -- openapi.json

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
