SERVER := server
UV := uv --directory $(SERVER)
PNPM := pnpm

.DEFAULT_GOAL := help

# Every target is a task, never a file — `corpus` in particular collides with the
# directory of the same name, and make would otherwise consider it already built.
.PHONY: help install lint format typecheck test test-unit e2e corpus spec-lint matrix \
	check licenses notice audit verify-gates openapi build storybook migrate run clean \
	release release-preview up down compose-migrate

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies from the lockfiles
	$(UV) sync --all-groups
	$(PNPM) install

lint: ## Check lint rules and formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(PNPM) run lint
	$(PNPM) run format:check

format: ## Apply safe lint fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	$(PNPM) run format

typecheck: ## Static type check
	$(UV) run pyright
	$(PNPM) run typecheck

test: ## Run the unit and integration suites (needs Docker for the database)
	$(UV) run pytest --cov --fr-report=../traceability-report.json
	$(PNPM) run test

test-unit: ## Run only the tests that need no container
	$(UV) run pytest -m "not integration"
	$(PNPM) run test

e2e: ## Run the browser tests headless
	$(PNPM) --filter @store-everything/web run e2e

corpus: ## Regenerate the corpus fixtures, manifest hashes and attribution
	$(UV) run python ../corpus/generate.py
	$(UV) run python -m tools.corpus --refresh --attribution ../corpus/ATTRIBUTION.md

spec-lint: ## Check the specification documents against their authoring rules
	$(UV) run python -m tools.spec_lint

matrix: ## Build the requirement traceability matrix from the last test run
	$(UV) run python -m tools.traceability \
		--report ../traceability-report.json --output ../traceability-matrix.md

check: lint spec-lint typecheck test matrix e2e ## Everything the pipeline will check

licenses: ## Check dependency licences against the policy
	$(UV) run python -m tools.check_licenses

notice: ## Generate the third-party licence notice
	$(UV) run python -m tools.check_licenses --notice ../THIRD-PARTY-LICENSES.md

audit: ## Scan dependencies for known vulnerabilities
	$(UV) export --no-dev --no-emit-project --no-hashes --no-annotate \
		--format requirements-txt > /tmp/store-everything-requirements.txt
	uvx pip-audit --requirement /tmp/store-everything-requirements.txt
	$(PNPM) audit --prod --audit-level moderate

verify-gates: ## Prove every pipeline gate rejects a violating sample
	./tools/verify-gates.sh

openapi: ## Regenerate the contract and the typed client from the code
	$(UV) run python -m tools.export_openapi
	$(PNPM) run generate:api

build: ## Production build of the web app
	$(PNPM) run build

storybook: ## Serve the component showcase
	$(PNPM) --filter @store-everything/web run storybook

migrate: ## Apply database migrations
	$(UV) run alembic upgrade head

run: ## Serve the API locally
	$(UV) run python -m store_everything

release: ## Derive the next version from the commits, write the changelog, tag it
	uvx --from commitizen cz bump

release-preview: ## Show what `make release` would do, changing nothing
	uvx --from commitizen cz bump --dry-run --yes

up: ## Start the stack for development (no proxy, ports on localhost)
	docker compose -f compose.yaml -f compose.dev.yaml up -d --build

down: ## Stop the development stack
	docker compose -f compose.yaml -f compose.dev.yaml down

compose-migrate: ## Apply migrations inside the running stack
	docker compose -f compose.yaml -f compose.dev.yaml run --rm migrations

clean: ## Remove caches and build artefacts
	rm -rf $(SERVER)/.pytest_cache $(SERVER)/.ruff_cache $(SERVER)/.coverage \
		$(SERVER)/htmlcov $(SERVER)/dist $(SERVER)/build \
		web/dist web/coverage web/storybook-static web/playwright-report web/test-results
	find $(SERVER) -name '__pycache__' -type d -prune -exec rm -rf {} +
