# Developer entry points. CI runs the same commands as `lint`, `typecheck`, `test` and
# `test-integration`, so a green `make check` locally means CI's lint, type and unit checks
# pass with the same flags.

.PHONY: help install lint format typecheck test coverage test-integration check staging-init \
	lifecycle-check stack-up stack-down stack-reset migrate image

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install the project and its dev dependencies
	uv sync

lint: ## Static lint, format check, and workflow database-access audit
	uv run ruff check .
	uv run ruff format --check .
	uv run python -m curate_d.runtime.workflow_audit src/curate_d

format: ## Apply formatting and autofixable lint
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Strict type check
	uv run mypy

test: ## Isolated unit suite with the coverage gate, in parallel
	uv run pytest -n auto tests/unit

# PyCharm imports the coverage.py data file itself (Run | Manage Coverage Reports) and
# writes a same-named .xml beside it on import.
coverage: ## Unit coverage data in curate-d.coverage, importable by PyCharm
	COVERAGE_FILE=curate-d.coverage uv run pytest -n auto tests/unit

# Integration coverage deliberately does not count toward the floor: these tests are
# slower and flakier, and letting them cover the gap would hide untested edge cases.
test-integration: ## Integration suite (needs the local stack); no coverage gate
	uv run pytest tests/integration --no-cov -m integration

check: lint typecheck test ## Lint, type check and unit suite; CI adds staging integration

stack-up: ## Start the local stack (PostgreSQL, RustFS, API and workers) and wait for it
	docker compose up -d --wait

# Separate from stack-up so a staging problem is diagnosable on its own: this prints the
# provisioning steps and fails on the first one that cannot be verified.
staging-init: ## Re-run RustFS staging provisioning and its access-matrix check
	docker compose up rustfs-init

LIFECYCLE_COMPOSE = CURATE_D_RUSTFS_LIFECYCLE_PORT=$(LIFECYCLE_PORT) docker compose -p curate-d-lifecycle -f compose.yaml -f compose.lifecycle.yaml
LIFECYCLE_PORT ?= 9110

# A disposable RustFS with a one-second lifecycle day, in its own Compose project so the
# scaled clock never touches the development stack. Torn down with its data after the
# check, whether it passes or fails.
lifecycle-check: ## Observe RustFS lifecycle expiry and abort (rows 30/31) in seconds
	$(LIFECYCLE_COMPOSE) up -d --wait rustfs
	$(LIFECYCLE_COMPOSE) run --rm rustfs-init
	CURATE_D_TEST_RUSTFS_ENDPOINT=http://127.0.0.1:$(LIFECYCLE_PORT) \
	CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS=60 \
	uv run pytest tests/integration/test_staging_backend_checks.py --no-cov -m integration \
		--staging-backend=rustfs \
		-k "test_expiration_removes_an_aged_object or test_stale_multipart_is_reclaimed" \
		$(LIFECYCLE_PYTEST_ARGS); \
	status=$$?; $(LIFECYCLE_COMPOSE) down -v; exit $$status

migrate: ## Apply migrations to the configured database
	uv run alembic upgrade head

image: ## Build the container image
	docker build -f docker/Dockerfile -t curate-d:dev .

stack-down: ## Stop the local stack, keeping its data
	docker compose down

stack-reset: ## Destroy the local stack and its data
	docker compose down -v
