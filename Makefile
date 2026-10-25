# Developer entry points. CI runs the same commands as `lint`, `typecheck` and `test`, so a
# green `make check` locally means CI's lint, type and unit checks pass with the same
# flags. A pull request that changes a path the suite depends on also runs
# `test-integration`'s suite in shards, each on its own stack, with RustFS as the only
# staging backend.

.PHONY: help install lint format typecheck test coverage test-stack test-integration \
	test-durations check staging-init lifecycle-check stack-up stack-down stack-reset \
	migrate image

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

# Only what the suite talks to, migrated, as integration.yml starts it: never the API and
# workers. A launched worker applies the persisted schedules, and every DBOS process, the
# suite's included, then fires them onto the version the suite's fixtures make latest: a
# recovery or refresh tick would run against the test rows. So it starts from stack-reset,
# which destroys any earlier stack-up's containers, schedules and data. Compose
# interpolates the encryption key for every service, so integration.yml's public dummy stands
# in when none is exported; none of these services reads it.
TEST_STACK_COMPOSE = CURATE_D_CREDENTIAL_ENCRYPTION_KEY="$${CURATE_D_CREDENTIAL_ENCRYPTION_KEY:-MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=}" docker compose

# integration.yml sets both: `--no-build` is a no-op for these image-only services, and uv's
# would block a developer whose platform has no wheel for a dependency.
TEST_STACK_UP_FLAGS ?=
TEST_STACK_UV_FLAGS ?= --locked

# The shard count `make test-integration GROUP=<n>` reproduces. integration.yml's default
# (`vars.INTEGRATION_SHARDS || 4`) must match; tests/unit/infrastructure/
# test_integration_workflow.py pins the two together.
SPLITS ?= 4

test-stack: ## Reset the local stack, then start only the integration suite's services and migrate
	$(MAKE) stack-reset
	$(TEST_STACK_COMPOSE) up -d --wait $(TEST_STACK_UP_FLAGS) postgres rustfs rclone
	$(TEST_STACK_COMPOSE) run --rm rustfs-init
	uv run $(TEST_STACK_UV_FLAGS) alembic upgrade head

# Integration coverage deliberately does not count toward the floor: these tests are
# slower and flakier, and letting them cover the gap would hide untested edge cases.
# GROUP mode forces RustFS: CI's collection is RustFS-only, and the split is a function of
# the collection, so a reproduced shard must collect the same ids.
test-integration: ## Integration suite (needs `make test-stack`); GROUP=<n> reruns one CI shard; no coverage gate
	uv run pytest tests/integration --no-cov -m integration$(if $(GROUP), --staging-backend=rustfs --splits $(SPLITS) --group $(GROUP) --splitting-algorithm least_duration)

# Generated with CI's backend flag so the node ids match. Drift only affects shard balance.
test-durations: ## Regenerate .test_durations from a whole run; needs make test-stack
	uv run pytest tests/integration --no-cov -m integration --staging-backend=rustfs \
		--store-durations --clean-durations

check: lint typecheck test ## Lint, type check and unit suite; PRs add the integration suite when they change its inputs

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
	$(TEST_STACK_COMPOSE) down -v
