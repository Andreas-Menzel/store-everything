SERVER := server
UV := uv --directory $(SERVER)
PNPM := pnpm

.DEFAULT_GOAL := help

.PHONY: help install lint format typecheck test test-unit e2e check openapi build storybook migrate run clean

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
	$(UV) run pytest --cov
	$(PNPM) run test

test-unit: ## Run only the tests that need no container
	$(UV) run pytest -m "not integration"
	$(PNPM) run test

e2e: ## Run the browser tests headless
	$(PNPM) --filter @store-everything/web run e2e

check: lint typecheck test e2e ## Everything the pipeline will check

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

clean: ## Remove caches and build artefacts
	rm -rf $(SERVER)/.pytest_cache $(SERVER)/.ruff_cache $(SERVER)/.coverage \
		$(SERVER)/htmlcov $(SERVER)/dist $(SERVER)/build \
		web/dist web/coverage web/storybook-static web/playwright-report web/test-results
	find $(SERVER) -name '__pycache__' -type d -prune -exec rm -rf {} +
