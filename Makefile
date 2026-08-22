SERVER := server
UV := uv --directory $(SERVER)
PNPM := pnpm

.DEFAULT_GOAL := help

# Every target is a task, never a file — `corpus` in particular collides with the
# directory of the same name, and make would otherwise consider it already built.
.PHONY: help install lint format typecheck test test-unit e2e corpus spec-lint matrix \
	check check-staged licenses notice audit verify-gates openapi build storybook migrate run clean \
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

# One report per test layer, all three merged: a requirement the browser suite proves must
# reach the same gate as one the core suite proves (11 § requirement traceability).
matrix: ## Build the requirement traceability matrix from the last full test run
	$(UV) run python -m tools.traceability \
		--report ../traceability-report.json \
		--report ../traceability-report.web.json \
		--report ../traceability-report.e2e.json \
		--output ../traceability-matrix.md

# `matrix` runs last on purpose: it reads what every suite before it wrote.
check: lint spec-lint typecheck test e2e matrix ## Everything the pipeline will check

# `check` runs against the working tree, which is not what gets pushed. This runs the
# pipeline's own steps against the *staged* tree, exported to a scratch directory with a
# cold environment built from the committed lockfile — the difference that once let a
# commit missing every staged modification (and a lockfile entry) reach CI green locally.
STAGED_TREE := $(CURDIR)/.check-staged
check-staged: ## Run the pipeline against the staged tree, in a clean cold checkout
	@rm -rf $(STAGED_TREE)
	@mkdir -p $(STAGED_TREE)
	@git checkout-index -a -f --prefix=$(STAGED_TREE)/
	@echo "--> cold install from the staged lockfile"
	@cd $(STAGED_TREE) && uv sync --directory server --all-groups --locked
	@cd $(STAGED_TREE) && uv run --directory server ruff check .
	@cd $(STAGED_TREE) && uv run --directory server ruff format --check .
	@cd $(STAGED_TREE) && uv run --directory server pyright
	@cd $(STAGED_TREE) && uv run --directory server pytest -q --cov \
		--fr-report=../traceability-report.json
	@cd $(STAGED_TREE) && uv run --directory server python -m tools.spec_lint
	@cd $(STAGED_TREE) && uv run --directory server python -m tools.check_licenses
	@rm -rf $(STAGED_TREE)
	@echo "--> the staged tree passes the pipeline's server, contract and docs checks"

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
		web/dist web/coverage web/storybook-static web/playwright-report web/test-results \
		.check-staged
	find $(SERVER) -name '__pycache__' -type d -prune -exec rm -rf {} +
