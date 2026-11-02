# Design

## Context

See proposal.md - Why. The pieces the runner builds on, as they are today:

- `make test-stack` resets the default Compose project (`curate-d`), starts `postgres`,
  `rustfs` and `rclone`, runs `rustfs-init` and `alembic upgrade head`. Compose needs
  `CURATE_D_CREDENTIAL_ENCRYPTION_KEY` to interpolate the file, so `TEST_STACK_COMPOSE` supplies
  CI's public dummy when none is exported.
- Every port those services publish comes from a variable: `CURATE_D_POSTGRES_PORT` (5433),
  `CURATE_D_RUSTFS_PORT` (9100), `CURATE_D_RUSTFS_CONSOLE_PORT` (9101), `CURATE_D_RCLONE_PORT`
  (5572). Since `api-loopback-bind`, archived 2026-10-31, all publish on `127.0.0.1`.
- The suite finds its stack through `CURATE_D_LEDGER_DATABASE_URL`, `CURATE_D_TEST_RUSTFS_ENDPOINT`
  and `CURATE_D_TEST_RCLONE_URL`, except `test_staging_occupancy.py`, which hardcodes 9100.
- `lifecycle-check` already runs a second stack beside the default one (`-p curate-d-lifecycle`,
  RustFS on 9110).
- CI's shard step: `uv run --locked --no-build pytest tests/integration --no-cov -m integration
  --staging-backend=rustfs --splits N --group i --splitting-algorithm least_duration
  --junitxml=integration-i.xml -o junit_family=legacy`.
- The host is macOS with GNU Make 3.81; Docker has 12 CPUs and 8 GB here.

## Goals / Non-Goals

**Goals:**
- One command that runs the whole suite as CI's shards, each against its own stack, at once.
- The same tests per shard as CI's shard of the same number, so a failure here reproduces with
  `make test-integration GROUP=<n>` and in CI.
- No residue: every shard stack is removed on success, failure and interrupt.

**Non-Goals:**
- Changing CI. CI keeps one shard per runner.
- Running shards in parallel on one database (pytest-xdist). That is unsafe by design.
- The boot job's checks (application boot, schedules). CI runs them on their own stack.
- The OVH backend. As in CI, the shards run with RustFS only.
- Choosing the shard count from the host's CPU or memory.

## Decisions

### D1. One Compose project per shard

Shard *i* runs as Compose project `curate-d-it-<i>` from the same `compose.yaml`, which gives it
its own containers, network and named volumes. That is the isolation CI gets from one runner
per shard, and it inherits every advisory-lock and DBOS-version guarantee the suite already
assumes. The default project is never named, so a `make stack-up` stack is untouched (the
precedent is `lifecycle-check`).

Alternative: `pytest -n` on one stack. Rejected; see proposal.md - Why.

### D2. A Python runner behind the Make target

`make test-integration-parallel` runs `uv run $(TEST_STACK_UV_FLAGS) python -m
tests.parallel_integration --splits $(SPLITS)`. The runner is standard-library Python and
imports nothing from `curate_d`. The plan for each shard (project name, ports, environment and
command lines) is a pure function. A thin executor runs the plan with one thread per shard and
`subprocess`.

Alternative: GNU Make `-j -k` over pattern targets `integration-shard-%`. Rejected: port
arithmetic and teardown on interrupt in recipe shell are hard to read. Make 3.81, which macOS
ships, has no `--output-sync`. Recipes cannot be unit-tested, and the project requires an
isolated unit test for each unit of behavior.

The runner lives under `tests/` because it is test tooling, not an operational asset (`ops/`) or
product code (`src/`). Pytest collects only `test_*.py`, so it does not collect the runner.

### D3. A fixed port block per shard

| Service | Shard *i* host port | Range for 1 to 32 |
| --- | --- | --- |
| PostgreSQL | 5440 + *i* | 5441-5472 |
| RustFS API | 9120 + 2*i* | 9122-9184 |
| RustFS console | 9121 + 2*i* | 9123-9185 |
| rclone | 5580 + *i* | 5581-5612 |

None of these overlaps the default stack (5433, 9100, 9101, 5572, 8000), the lifecycle stack
(9110) or another shard. A unit test pins that for every *i* from 1 to 32. A port that another
process already holds fails that shard's `up` step, which names the port (spec scenario "A shard
stack does not start"). Fixed ports keep a shard's addresses predictable in its log.

Alternative: ask the OS for free ports. Rejected: the addresses are then unpredictable, and the
port can still be taken between the choice and `up`.

### D4. Shard steps and teardown

Each shard runs these steps in order. A failed step stops the shard before the next step:

1. `docker compose -p curate-d-it-<i> down -v`. This removes the residue of an earlier run
   that was killed. It is the per-shard equivalent of `stack-reset`.
2. `docker compose -p curate-d-it-<i> up -d --wait postgres rustfs rclone`.
3. `docker compose -p curate-d-it-<i> run --rm rustfs-init`.
4. `alembic upgrade head`, with the shard's DSN.
5. CI's shard command, with the shard's environment, with `--splits` and `--group` set for the
   shard, and with `--junitxml` pointed into the shard's output directory.
   `-o junit_family=legacy` stays, so the report has CI's shape.

Steps 4 and 5 run `alembic` and `pytest` from the environment `uv run` gave the runner, not
through a nested `uv run`.

After step 5, or after any failed step, the runner writes `docker compose logs` for the shard
when the shard failed. Then it always runs `down -v`. The shard environment sets
`CURATE_D_LEDGER_DATABASE_URL=postgresql+psycopg://curate_d:curate_d@localhost:<pg>/curate_d`,
`CURATE_D_TEST_RUSTFS_ENDPOINT=http://127.0.0.1:<rustfs>` and
`CURATE_D_TEST_RCLONE_URL=http://127.0.0.1:<rclone>`. It sets the four port variables and
`CURATE_D_CREDENTIAL_ENCRYPTION_KEY`, with the same default as `TEST_STACK_COMPOSE`. All shards
start at once. Docker pulls each image once, and the images are only those of `make test-stack`.

To debug a failed shard, the developer reruns it with `make test-stack && make test-integration
GROUP=<n>`. That is the existing requirement, so the runner does not keep stacks.

### D5. Output and exit code

Each run writes to `.integration-runs/<UTC timestamp>/shard-<i>/`: `stack.log` (the compose
steps and logs), `pytest.log` and `junit.xml`. The directory is git-ignored. Each shard's output
goes to its own files, not the terminal. The terminal shows one line when each shard starts and
finishes, and then a summary: shard, result, failed step, duration, and the paths of its `pytest.log`
and `junit.xml`. The runner
exits 0 only when every shard passes. Otherwise it exits 1 and names each failed shard.

### D6. Interrupt

On SIGINT or SIGTERM, the runner terminates the running child processes. Then it runs `down -v`
for every shard project it started, and exits 130 (SIGINT) or 143 (SIGTERM). A second SIGINT
during teardown is ignored. Step 1 of the next run removes anything that a hard kill (SIGKILL)
leaves.

### D7. Shard count

`SPLITS` keeps its one Makefile default (4), which `test_integration_workflow.py` pins to CI's
default. The runner accepts an integer from 1 to 32, the range CI's plan accepts. It refuses any
other value before it starts a stack.

## Risks / Trade-offs

- [Four stacks use more memory than Docker has] → Each stack is PostgreSQL
  (`max_connections=100`), RustFS and rclone. Task 4.2 measures the peak use during a run with
  `docker stats` and records it with the wall time. A developer with less memory runs
  `SPLITS=2`.
- [Startup costs more than it saves] → Starting a stack takes tens of seconds, and the serial
  run took 17m18s. Task 4.2 records the parallel wall time. If it is not clearly lower, the
  change goes back to design before merge.
- [Shards contend for host CPU] → pytest runs on the host, four processes on 12 CPUs. Tests
  with timing bounds (for example `_BLOCKED_FOR` in `test_staging_occupancy.py`) can get slower.
  A test that fails only in parallel is a finding to report, not a reason to change the test's
  bound in this change.
- [A future test hardcodes a stack address] → It would reach the default stack and fail in
  parallel. A unit test fails the build when a line under `tests/integration/` contains a
  literal `127.0.0.1:<port>` or `localhost:<port>` and is not an `os.environ.get(...)` default.
  For this rule, `test_staging_contract.py` and `test_staging_backend_checks.py` take their
  `_LOCAL_ENDPOINTS` RustFS value from `rustfs_staging.ENDPOINT`, which already reads
  `CURATE_D_TEST_RUSTFS_ENDPOINT`. Their behavior stays the same.
- [Leftover `curate-d-it-*` projects after a hard kill] → Step 1 of the next run removes them.
  The README line names `docker compose -p curate-d-it-<i> down -v` for manual cleanup.

## Migration Plan

Additive. No existing target, workflow or test command changes. Rollback is to revert the
change.
