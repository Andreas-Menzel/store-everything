SERVER := server
UV := uv --directory $(SERVER)

.DEFAULT_GOAL := help

.PHONY: help install lint format typecheck test test-unit check migrate run clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies from the lockfile
	$(UV) sync --all-groups

lint: ## Check lint rules and formatting
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Apply safe lint fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## Static type check
	$(UV) run pyright

test: ## Run the full suite with coverage (needs Docker for integration tests)
	$(UV) run pytest --cov

test-unit: ## Run only the tests that need no container
	$(UV) run pytest -m "not integration"

check: lint typecheck test ## Everything the pipeline will check

migrate: ## Apply database migrations
	$(UV) run alembic upgrade head

run: ## Serve the API locally
	$(UV) run python -m store_everything

clean: ## Remove caches and build artefacts
	rm -rf $(SERVER)/.pytest_cache $(SERVER)/.ruff_cache $(SERVER)/.coverage \
		$(SERVER)/htmlcov $(SERVER)/dist $(SERVER)/build
	find $(SERVER) -name '__pycache__' -type d -prune -exec rm -rf {} +
