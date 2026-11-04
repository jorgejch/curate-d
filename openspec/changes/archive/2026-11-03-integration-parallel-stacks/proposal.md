# Proposal

## Why

A whole local integration run is serial and took 17m18s on 2026-10-31. CI already runs the
suite as four shards, each on its own stack, but locally a developer can only run the whole
suite or one shard (`make test-integration GROUP=<n>`) against the single `make test-stack`
stack. `pytest -n auto` on that stack is not an option: a shard's tests share one database's
advisory locks and DBOS's latest application version (`pyproject.toml`, pytest-xdist note), so
two workers on one database would interfere.

## What Changes

- New Make target `test-integration-parallel` runs `SPLITS` shards (default 4, the count CI
  uses) at once. Each shard has its own Compose project and its own loopback ports, and is
  provisioned and migrated. Each shard runs CI's shard command (`--staging-backend=rustfs
  --splits N --group i --splitting-algorithm least_duration`) serially, against its own stack.
- Every shard runs to completion even when another fails. The target exits non-zero when any
  shard fails, names the failing shards and keeps every shard's log and JUnit report. It
  removes each shard's stack and data afterwards, and also on an interrupt.
- The target never touches the default `curate-d` Compose project, so a `make stack-up` stack
  keeps running beside it.
- `tests/integration/test_staging_occupancy.py` reads `CURATE_D_TEST_RUSTFS_ENDPOINT` instead of
  a hardcoded `http://127.0.0.1:9100`. It is the only integration module that cannot be pointed
  at another stack today. `test_staging_contract.py` and `test_staging_backend_checks.py` take
  their RustFS default from the same place, `rustfs_staging.ENDPOINT`. A unit test keeps every
  literal stack address in `tests/integration/` inside an environment-variable default.
- `README.md` and `CONTRIBUTING.md` list the target.

`make test-integration`, `GROUP` and `make test-stack` do not change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: adds the requirement "The integration suite runs locally across parallel
  isolated stacks", beside "Shard failures are reproducible locally", which owns the existing
  single-shard target.

## Impact

- New: `tests/parallel_integration.py` (the shard runner), its unit tests, and the Make target.
- Changed: `tests/integration/test_staging_occupancy.py`, `test_staging_contract.py`,
  `test_staging_backend_checks.py`, `Makefile`, `README.md`, `CONTRIBUTING.md`, `.gitignore`
  (the run output directory).
- CI: unchanged. CI keeps one shard per runner. The runner is local tooling.
- Host resources: four stacks of PostgreSQL, RustFS and rclone. This machine gives Docker 12
  CPUs and 8 GB.
- Builds on `api-loopback-bind`, archived 2026-10-31, whose loopback-only publishing the
  shard stacks inherit.
