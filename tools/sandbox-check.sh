#!/usr/bin/env bash
# Prove the extractor sandbox, from inside it.
#
# The promise (ADR-0021, 00 § security posture) is that an extractor container can reach the
# extractor API and nothing else. That is a property of the *deployment* — a compose network with
# no gateway — so it cannot be tested from the application's own test suite: the only honest place
# to ask is inside a running container.
#
# Four questions, asked from the extractor itself:
#
#   1. can it reach the core?              it must — otherwise it could not work at all
#   2. can it reach PostgreSQL?            it must not — the database is not on its network
#   3. can it reach the internet by IP?    it must not — the network has no gateway
#   4. can it resolve a public name?       it must not — this is CVE-2024-29018's hole
#
# The first is as important as the other three: a container that can reach nothing at all would
# pass a naive isolation test while being useless, so the check has a positive control.
#
#   ./tools/sandbox-check.sh            # brings the stack up, checks, tears it down
#   KEEP_STACK=1 ./tools/sandbox-check.sh   # leaves it running to poke at
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Its own project name, which is not a nicety: compose namespaces containers, networks *and
# volumes* by project, and the base file's name is the one a developer's real stack uses. Without
# this, the teardown below would delete somebody's database.
PROJECT="${SANDBOX_PROJECT:-store-everything-sandbox}"
COMPOSE=(
  docker compose -p "$PROJECT"
  -f compose.yaml -f compose.dev.yaml -f compose.extractor-example.yaml
)
ENV_FILE="$(mktemp -t sandbox-env-XXXXXX)"
ADMIN_EMAIL="sandbox@example.com"
ADMIN_PASSWORD="sandbox-password-1"
API_PORT="${SANDBOX_API_PORT:-8099}"
KEEP_STACK="${KEEP_STACK:-0}"

failures=0

note() { printf '\n\033[36m==>\033[0m %s\n' "$1"; }
ok() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
fail() {
  printf '  \033[31mFAIL\033[0m  %s\n' "$1"
  failures=$((failures + 1))
}

cleanup() {
  if [ "$KEEP_STACK" = "1" ]; then
    printf '\nstack left running (KEEP_STACK=1); tear it down with:\n  %s down -v\n' \
      "${COMPOSE[*]} --env-file $ENV_FILE"
    return
  fi
  note "tearing the sandbox stack down"
  "${COMPOSE[@]}" --env-file "$ENV_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
}
trap cleanup EXIT

# An environment of its own, so a developer's `.env` is neither read nor written. The database
# password is a throwaway for a stack that is destroyed at the end of this script.
cat > "$ENV_FILE" <<EOF
POSTGRES_USER=sandbox
POSTGRES_PASSWORD=sandbox
POSTGRES_DB=sandbox
PUBLIC_HOST=sandbox.localhost
API_PORT=$API_PORT
POSTGRES_PORT=${SANDBOX_POSTGRES_PORT:-55432}
SE_BOOTSTRAP_ADMIN_EMAIL=$ADMIN_EMAIL
SE_BOOTSTRAP_ADMIN_PASSWORD=$ADMIN_PASSWORD
SE_SESSION_COOKIE_SECURE=false
SE_REFERENCE_TOKEN=placeholder
EOF

compose() { "${COMPOSE[@]}" --env-file "$ENV_FILE" "$@"; }

note "building and starting the core"
compose build api reference-extractor >/dev/null
compose up -d postgres >/dev/null
compose run --rm migrations >/dev/null
compose up -d api >/dev/null

note "waiting for the API to be ready"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$API_PORT/readyz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$API_PORT/readyz" >/dev/null

note "provisioning the reference extractor"
COOKIES="$(mktemp -t sandbox-cookies-XXXXXX)"
curl -fsS -c "$COOKIES" -X POST "http://127.0.0.1:$API_PORT/api/v1/auth/login" \
  -H 'Content-Type: application/json' -H "Origin: http://127.0.0.1:$API_PORT" \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" >/dev/null

TOKEN="$(curl -fsS -b "$COOKIES" -X POST "http://127.0.0.1:$API_PORT/api/v1/extractors" \
  -H 'Content-Type: application/json' -H "Origin: http://127.0.0.1:$API_PORT" \
  -d '{"id":"reference"}' | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
rm -f "$COOKIES"
if [ -z "$TOKEN" ]; then
  echo "could not mint an extractor credential" >&2
  exit 2
fi
sed -i.bak "s|^SE_REFERENCE_TOKEN=.*|SE_REFERENCE_TOKEN=$TOKEN|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"

note "starting the extractor"
compose up -d reference-extractor >/dev/null

# Give it a moment to register, which is also the positive control: an extractor that cannot
# reach the core never gets a manifest recorded.
registered=0
for _ in $(seq 1 30); do
  if compose logs reference-extractor 2>/dev/null | grep -q "registered reference"; then
    registered=1
    break
  fi
  sleep 1
done

note "asking the extractor what it can reach"

# Run a probe *inside* the extractor container. Its image is python-slim and its root filesystem
# is read-only, which is the shape a real extractor runs in — so the probes are stdlib only.
probe() {
  compose exec -T reference-extractor python -c "$1" 2>&1
}

if [ "$registered" = "1" ]; then
  ok "the extractor reached the core and registered (the positive control)"
else
  fail "the extractor never registered — it cannot reach the core, so nothing below means much"
fi

if probe '
import socket, sys
with socket.create_connection(("api", 8000), timeout=5):
    print("reached")
' | grep -q reached; then
  ok "the core is reachable from inside the extractor"
else
  fail "the core is NOT reachable from inside the extractor"
fi

if probe '
import socket
try:
    with socket.create_connection(("postgres", 5432), timeout=5):
        print("reached")
except OSError as refused:
    print("refused:", type(refused).__name__)
' | grep -q reached; then
  fail "PostgreSQL is reachable from an extractor — it must not be on that network"
else
  ok "PostgreSQL is unreachable from the extractor"
fi

if probe '
import socket
try:
    with socket.create_connection(("1.1.1.1", 443), timeout=5):
        print("reached")
except OSError as refused:
    print("refused:", type(refused).__name__)
' | grep -q reached; then
  fail "the extractor reached the internet by IP — the network has a gateway it should not have"
else
  ok "the internet is unreachable by IP (no gateway)"
fi

if probe '
import socket
try:
    print("resolved", socket.gethostbyname("example.com"))
except OSError as refused:
    print("refused:", type(refused).__name__)
' | grep -q resolved; then
  fail "a public name resolved inside the extractor — check the Docker Engine version (>= 25.0.5)"
else
  ok "public names do not resolve (CVE-2024-29018 is not present)"
fi

if probe '
from pathlib import Path
try:
    Path("/etc/probe").write_text("x")
    print("wrote")
except OSError as refused:
    print("refused:", type(refused).__name__)
' | grep -q wrote; then
  fail "the extractor wrote to its root filesystem, which should be read-only"
else
  ok "the root filesystem is read-only"
fi

if [ "$(probe 'import os; print(os.getuid())' | tr -d "\r")" = "0" ]; then
  fail "the extractor runs as root"
else
  ok "the extractor runs unprivileged"
fi

printf '\n'
if [ "$failures" -gt 0 ]; then
  printf '\033[31m%d sandbox check(s) failed\033[0m\n' "$failures"
  exit 1
fi
printf '\033[32mthe extractor sandbox holds\033[0m\n'
