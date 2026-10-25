# Tasks

Stream A owns the shared edits (`pyproject.toml`, `uv.lock`, `Makefile`, `.test_durations`)
and starts first; 1.1 and 1.2 are short, 1.3 and 1.4 are local stack runs of about ten
minutes each. Streams B (`.github/workflows/integration.yml`) and C
(`tests/unit/infrastructure/test_integration_workflow.py`, `CONTRIBUTING.md`,
`docs/staging.md`) touch disjoint files and can start as soon as 1.1 and 1.2 have landed.
Stream C's pin tests close green only once stream B's workflow and 1.3's durations file
exist. Job ids, names, step names, outputs and the shard command are fixed in design.md so
B and C can be written against the same contract. Group 4 waits on everything.

## 1. Stream A: dependency, Make targets, durations (`pyproject.toml`, `uv.lock`, `Makefile`, `.test_durations`)

- [x] 1.1 Add `pytest-split~=0.11` to the `dev` dependency group with a one-line comment
  (splits the integration suite across CI shards by recorded durations), reword the
  `pytest-xdist` comment to say the integration suite stays serial per shard and is split
  across isolated stacks instead, and refresh the lock with `uv lock`; verify with
  `uv sync --locked && uv run --locked pytest --help | grep -E -- '--splits|--group|--store-durations'`
  listing all three options
- [x] 1.2 In the `Makefile`: add `SPLITS ?= 4` with a comment that the workflow's default
  must match; make `test-integration` run
  `uv run pytest tests/integration --no-cov -m integration` plus
  `--staging-backend=rustfs --splits $(SPLITS) --group $(GROUP) --splitting-algorithm least_duration`
  only when `GROUP` is set (CI's collection is RustFS-only, so a reproduced shard must be
  too); add `test-durations` (`## Regenerate .test_durations from a whole run;
  needs make test-stack`) running the whole suite with `--staging-backend=rustfs
  --store-durations --clean-durations`; add both to `.PHONY`; reword the header's last
  sentence to say the suite runs in shards on isolated stacks; verify with
  `make -n test-integration` (no `--splits`, no `--staging-backend`),
  `make -n test-integration GROUP=2`
  (`--staging-backend=rustfs --splits 4 --group 2 --splitting-algorithm least_duration`) and
  `make -n test-durations` (`--store-durations --clean-durations --staging-backend=rustfs`)
- [x] 1.3 Generate the durations: `make test-stack && make test-durations`, then commit
  `.test_durations`; verify the whole run passed, and that
  `python3 -c "import json; d=json.load(open('.test_durations')); assert d and all(k.startswith('tests/integration/') and v > 0 for k, v in d.items()); print(len(d))"`
  prints the same count as
  `uv run pytest tests/integration --collect-only -q -m integration --staging-backend=rustfs --no-cov | grep -c '::'`
- [x] 1.4 Prove the split is order-independent: for each group 1 to 4 run
  `make test-stack && make test-integration GROUP=<g>` against a fresh stack; verify every
  group passes, the
  four `[pytest-split] Running group g/4 (N tests)` counts sum to 1.3's count, and note the
  four wall times for the pull request description

## 2. Stream B: workflow (`.github/workflows/integration.yml`)

- [x] 2.1 Replace the `integration` job with a `plan` job (`name: Plan the integration
  run`, `timeout-minutes: 10`): checkout with `persist-credentials: false`, the budget
  guard, the `dorny/paths-filter` detection (`id: changes`, same SHA, same filter plus
  `.test_durations`), the undecided-detection guard, then a "Refuse an invalid shard
  count" step reading `${{ vars.INTEGRATION_SHARDS || 4 }}` from its `env`, failing with
  `::error::` unless it is an integer from 1 to 32, and writing `splits=<n>` and
  `groups=[1,...,n]` to `GITHUB_OUTPUT`; then, each `if: steps.changes.outputs.suite ==
  'true'`: install uv, `uv sync --locked --no-build`, and "Collect the suite" writing
  `collected=<count of node-id lines>` from
  `uv run --locked --no-build pytest tests/integration --collect-only -q -m integration --staging-backend=rustfs --no-cov`;
  declare `outputs: suite, splits, groups, collected`; verify with
  `uv run --locked python -c "import yaml; yaml.safe_load(open('.github/workflows/integration.yml'))"`
  and `actionlint .github/workflows/integration.yml` if installed
- [x] 2.2 Add the `shard` job: `name: Integration shard ${{ matrix.group }} of
  ${{ needs.plan.outputs.splits }}`, `needs: plan`, `if: needs.plan.outputs.suite ==
  'true'`, `timeout-minutes: 15`, `strategy: fail-fast: false` with `matrix: group:
  ${{ fromJSON(needs.plan.outputs.groups) }}`; steps: checkout (no credentials),
  Tesseract, uv, sync, "Start PostgreSQL, RustFS and rclone, and apply migrations"
  (`make test-stack` with `TEST_STACK_UP_FLAGS: --no-build` and `TEST_STACK_UV_FLAGS:
  --locked --no-build`), "Integration suite" with exactly design.md's shard command,
  "Upload the shard report" (`actions/upload-artifact@v7`, `if: always()`, name
  `integration-report-${{ matrix.group }}`, path `integration-${{ matrix.group }}.xml`,
  `if-no-files-found: ignore`), "Stack logs" on `failure()`, "Remove the stack" on
  `always()`; no Buildx or image build step; verify with the YAML load and actionlint
- [x] 2.3 Add the `boot` job: `name: Application boot`, `needs: plan`, `if:
  needs.plan.outputs.suite == 'true'`, `timeout-minutes: 15`; steps: checkout (no
  credentials), uv, sync, Buildx, build and load `curate-d:dev` (same action pins and
  cache settings as today), `make test-stack` with the same env flags, "Start the
  application services", "Wait for the workers to be ready", "Check the workers applied
  their schedules" (unchanged bodies), "Stack logs" on `failure()`, "Remove the stack" on
  `always()`; verify with the YAML load and actionlint
- [x] 2.4 Add the aggregate job `integration` (`name: Integration tests`, `needs: [plan,
  shard, boot]`, `if: always()`, `timeout-minutes: 5`): a first step "Refuse an incomplete
  run" implementing design.md's gate rule from `needs.plan.result`,
  `needs.plan.outputs.suite`, `needs.shard.result` and `needs.boot.result`, failing with
  `::error::` naming the job and result; "Suite not selected" (`if:
  needs.plan.outputs.suite == 'false'`, today's summary text plus `.test_durations` in
  the list); "Download the shard reports" (`actions/download-artifact@v8`, `pattern:
  integration-report-*`, `merge-multiple: true`, `path: reports`, `if:
  needs.plan.outputs.suite == 'true'`); "Check the shards ran the whole suite" (same
  `if`): exactly `splits` files `reports/integration-<g>.xml` for g in 1..splits, and the
  sum of their `testsuite` `tests` attributes equals `needs.plan.outputs.collected`, else
  `::error::` with the counts; "Record staging evidence" (`if: always() &&
  needs.plan.outputs.suite == 'true'`): today's script over every `reports/*.xml`, plus a
  per-shard line (tests, failures, errors, skipped, time); verify with the YAML load,
  actionlint, and by running the gate script locally with
  `needs`-shaped environment variables for the five cases in the spec's "The gate fails
  closed" scenarios
- [x] 2.5 Rewrite the header comment: the four jobs and why the required name sits on the
  aggregate, the per-runner stack and why ports cannot collide, serial per shard, the
  shard-count variable and its default, and the boot on its own stack; keep `on`,
  `concurrency`, `permissions` and `env` unchanged; verify with
  `uv run --locked pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov`
  once stream C has landed

## 3. Stream C: pin tests and docs (`tests/unit/infrastructure/test_integration_workflow.py`, `CONTRIBUTING.md`, `docs/staging.md`)

- [x] 3.1 Replace `_job()` with `_jobs()`, `_plan()`, `_shard()`, `_boot()` and `_gate()`
  accessors and `_steps(job)`, and rewrite the topology pins: the required name on
  `integration` only (`test_the_job_keeps_the_required_check_name`), the job set is exactly
  `{plan, shard, boot, integration}`, `shard` and `boot` have `needs: plan` and `if:
  needs.plan.outputs.suite == 'true'`, the gate has `needs` of all three and `if:
  always()`; verify with
  `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov` (red
  until stream B lands, green after)
- [x] 3.2 Rewrite `test_the_gate_detects_changes_inside_the_required_job` as
  `test_the_plan_detects_changes_before_installing_anything`: in `plan`, the detection
  follows the budget guard, the undecided guard follows it with no `if`, the shard-count
  guard follows that and names `vars.INTEGRATION_SHARDS || 4`, `1` and `32`, and every
  later `plan` step has an `if` containing `steps.changes.outputs.suite == 'true'` with no
  `||`; add `test_the_gate_fails_closed`: the gate's first step has no `if`, names
  `needs.plan.result`, `needs.shard.result`, `needs.boot.result` and
  `needs.plan.outputs.suite`, and exits non-zero; the "Suite not selected" step is in the
  gate with `== 'false'` and writes `GITHUB_STEP_SUMMARY`; verify with the same command
- [x] 3.3 Point the suite-command pins (`test_the_whole_suite_runs_with_nothing_deselected`,
  `test_rustfs_is_the_only_staging_backend`,
  `test_integration_coverage_never_reaches_sonarqube`) at the `shard` job and add
  `test_each_shard_runs_its_group_of_the_plans_split`: `fail-fast` is `False`, the matrix
  is `group: ${{ fromJSON(needs.plan.outputs.groups) }}`, the command has `--splits ${{
  needs.plan.outputs.splits }}`, `--group ${{ matrix.group }}`, `--splitting-algorithm
  least_duration`, `--junitxml=integration-${{ matrix.group }}.xml` and no `-n`; the
  upload step follows the suite with `if: always()`; `shard` has no `docker/build-push-action`
  step; verify with the same command
- [x] 3.4 Rewrite the stack and boot pins against the new jobs:
  `test_no_application_service_runs_beside_the_suite` over `shard` (compose `up` only
  with `postgres rustfs rclone`), `test_migrations_run_before_the_application_services_start`,
  `test_each_worker_must_log_ready_after_it_starts` and
  `test_the_gate_checks_the_workers_applied_their_schedules` over `boot` (the schedule
  check with no `if` and no `continue-on-error`),
  `test_ci_starts_the_suites_services_with_the_developers_target` and
  `test_ci_keeps_its_image_and_lockfile_guarantees_through_the_target` over both stack
  jobs, `test_the_stack_is_removed_on_every_outcome` over both, and
  `test_the_checkout_leaves_no_token_on_disk` over every job; add
  `test_the_gate_checks_the_shards_ran_the_whole_suite`: a download step with `pattern:
  integration-report-*` precedes a step naming `needs.plan.outputs.collected` and exiting
  non-zero; verify with the same command
- [x] 3.5 Add `test_the_shard_count_defaults_agree` (the Makefile's `SPLITS ?= <n>` equals
  the `n` in the plan's `vars.INTEGRATION_SHARDS || <n>`, and `n == 4`) and
  `test_the_durations_file_covers_the_integration_suite` (`.test_durations` exists, parses
  as a non-empty JSON object, every key starts with `tests/integration/`, every value is a
  positive number), and add `.test_durations` to `EXPECTED_DECLARED_PATHS`; verify with
  the same command and, for the durations test, by temporarily writing `{}` to a copy and
  seeing it fail naming the file
- [x] 3.6 Docs: in `CONTRIBUTING.md`'s "Tests" bullets say the pull request runs the
  suite in shards (default four, `INTEGRATION_SHARDS`), each on its own stack, how to rerun
  one shard (`make test-stack && make test-integration GROUP=<n>`) and when to run `make
  test-durations`; in `docs/staging.md` reword the "integration workflow runs the whole
  integration suite" sentence to say it runs in shards on isolated RustFS stacks and the
  `Integration tests` summary merges every shard's evidence; verify with
  `grep -n "GROUP=\|test-durations" CONTRIBUTING.md` and
  `grep -n "shard" docs/staging.md` each returning at least one line

## 4. Integration check

- [x] 4.1 Run `make lint typecheck test` and verify it passes with the coverage floor met
- [x] 4.2 Run `openspec validate parallelize-integration-tests --strict` and verify it
  passes
- [x] 4.3 Open the pull request against `dev`; verify the run shows `Plan the integration
  run`, `Integration shard 1 of 4` to `4 of 4`, `Application boot` and `Integration tests`
  all green, the `Integration tests` summary lists four shard lines whose test counts sum
  to the plan's collected count and the merged contract and backend-check counts, and
  record the run URL and the shard wall times in the pull request description
- [x] 4.4 Verify the fail-closed path once on the pull request: push a commit that sets
  `INTEGRATION_SHARDS`'s default expression to `0` (or temporarily fails one shard's test),
  confirm `Integration tests` fails naming the plan or the shard while the other shards
  finish, then revert the commit; record both run URLs in the pull request description

## Workflow follow-up

- After the merge into `dev`, confirm on the next docs-only pull request that `Integration
  tests` passes in about a minute with the shard and boot jobs skipped and the summary
  naming the declared paths.
- Synced into `openspec/specs/release-pipeline/spec.md` and archived in pull request #94,
  with `openspec validate --all --strict` passing.
