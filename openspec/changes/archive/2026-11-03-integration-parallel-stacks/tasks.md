# Tasks

Groups 1, 2 and 3 touch disjoint files and can start at once. Group 3's verification runs the
runner, so it finishes after group 2. Group 4 waits on all three. `api-loopback-bind` is archived, so `compose.yaml`
already publishes on loopback only. Nothing waits on it.

## 1. Stack addresses in the integration suite

- [x] 1.1 In `tests/integration/test_staging_occupancy.py`, replace `_ENDPOINT = "http://127.0.0.1:9100"` with the `ENDPOINT` that `tests.integration.rustfs_staging` exports, which reads `CURATE_D_TEST_RUSTFS_ENDPOINT`. Verify `make test-stack && uv run pytest tests/integration/test_staging_occupancy.py --no-cov -m integration --staging-backend=rustfs` passes
- [x] 1.2 In `tests/integration/test_staging_contract.py` and `tests/integration/test_staging_backend_checks.py`, set `_LOCAL_ENDPOINTS = {"rustfs": ENDPOINT}` from `tests.integration.rustfs_staging` (design, Risks). Verify both modules pass with the same command, and `make lifecycle-check` still passes
- [x] 1.3 Add `tests/unit/infrastructure/test_integration_stack_addresses.py`: every line under `tests/integration/` that contains `127.0.0.1:<port>` or `localhost:<port>` is an `os.environ.get(...)` default, and a failure names the file and line. Verify `uv run pytest tests/unit/infrastructure/test_integration_stack_addresses.py --no-cov` passes, and fails naming `test_staging_occupancy.py` when task 1.1 is reverted locally

## 2. Shard runner

- [x] 2.1 Add `tests/parallel_integration.py` with the pure plan (design D1, D3, D4, D7): `parse_splits` accepts an integer from 1 to 32 and otherwise raises `ValueError` naming `SPLITS` and the range; `shard_plan(i, splits, run_dir)` returns the project name `curate-d-it-<i>`, the port block, the environment (DSN, `CURATE_D_TEST_RUSTFS_ENDPOINT`, `CURATE_D_TEST_RCLONE_URL`, the four port variables, the encryption-key default) and the five step command lines, with the pytest line equal to CI's shard command with `--splits`, `--group` and `--junitxml` set for the shard. Add `tests/unit/infrastructure/test_parallel_integration.py`: valid and invalid `SPLITS` (`0`, `abc`, `2.5`, `33`); no port collides with the default stack, the lifecycle stack or another shard, for every `i` from 1 to 32; the pytest arguments match `integration.yml`'s shard step after its `--splits`, `--group` and `--junitxml` expressions are substituted. Verify `uv run pytest tests/unit/infrastructure/test_parallel_integration.py --no-cov` passes
- [x] 2.2 Add the executor (design D4, D5): one thread per shard runs the steps in order through an injected command runner, stops a shard at its first failed step, writes `docker compose logs` to `stack.log` for a failed shard, always runs `down -v`, and writes the summary; the exit code is 0 only when every shard passes. Unit-test it with a fake runner: all pass gives 0 and one `down -v` per shard; a pytest failure in shard 2 lets shards 1, 3 and 4 finish and names shard 2 and the paths of its `pytest.log` and `junit.xml` in the summary; a failed `up` skips that shard's migrations and pytest and names the step. Verify `uv run pytest tests/unit/infrastructure/test_parallel_integration.py --no-cov` passes
- [x] 2.3 Add interrupt handling (design D6): on SIGINT or SIGTERM, terminate the running children, run `down -v` for every shard project started, ignore a second SIGINT during teardown, and exit 130 or 143. Unit-test it with a fake runner and a simulated signal: every started project gets `down -v` and the exit code is 130. Verify `uv run pytest tests/unit/infrastructure/test_parallel_integration.py --no-cov` passes

## 3. Make target and documentation

- [x] 3.1 Add `test-integration-parallel` to the `Makefile` and its `.PHONY` list: `uv run $(TEST_STACK_UV_FLAGS) python -m tests.parallel_integration --splits $(SPLITS)`, with a `##` help line and a comment on why one stack per shard (design D1). Add `/.integration-runs/` to `.gitignore`. Verify `make help` lists the target, `make test-integration-parallel SPLITS=0` fails naming `SPLITS` and `docker compose ls` shows no `curate-d-it-*` project, and `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov` still passes
- [x] 3.2 Add the target to `README.md`'s Make target list and to `CONTRIBUTING.md`'s integration paragraph: what it runs, that it leaves a `make stack-up` stack running, where the logs go, `SPLITS=2` for a host with less memory, and `docker compose -p curate-d-it-<i> down -v` for manual cleanup. Verify `grep -c test-integration-parallel README.md CONTRIBUTING.md` finds both

## 4. Integration

- [x] 4.1 With a `make stack-up` stack running, run `make test-integration-parallel`. Verify that it exits 0, that `docker compose ls` shows no `curate-d-it-*` project afterwards, and that the default stack's containers and `127.0.0.1:8000/health/ready` are the same before and after
- [x] 4.2 Record the wall time of task 4.1 and the peak memory from `docker stats --no-stream` during the run. Compare the wall time with the serial 17m18s from 2026-10-31. If the parallel run is not clearly faster, stop and return to design (design, Risks)
- [x] 4.3 Add a temporary failing test file, not committed, under `tests/integration/`, and run the target. Verify that exactly one shard fails, that the other shards finish, that the exit code is non-zero, and that the summary names the shard and its `pytest.log` and `junit.xml`. Remove the file and verify `git status` does not show it
- [x] 4.4 Interrupt a run with Ctrl-C while the shards run pytest. Verify that the exit code is 130 and that `docker compose ls` and `docker volume ls --filter name=curate-d-it-` show nothing left
- [x] 4.5 Verify that `make lint`, `make typecheck`, `make test` and `openspec validate integration-parallel-stacks --strict` pass

## Workflow follow-up

- Put the measured wall time and memory from task 4.2 in the pull request description.
- Sync the delta into `openspec/specs/release-pipeline/spec.md` and archive the change.
