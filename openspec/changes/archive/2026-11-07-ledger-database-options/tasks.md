# Tasks

Streams A and B touch disjoint files and can start at once. Stream C touches only
`docs/staging.md` and can also start at once. Group 4 waits for all three.

## 1. Stream A: the setting (`src/curate_d/config.py`, `tests/unit/test_config.py`)

- [x] 1.1 Add `ledger_database_options` before `ledger_database_url`, with the default `""` and the field validator of design D2. Verify with tests that malformed options, an empty key, a repeated key and each rejected key stop settings load, that the error names the field, and that the error does not contain the value: `uv run pytest tests/unit/test_config.py --no-cov`.
- [x] 1.2 Make `_ledger_url` add the options as the URL query, encoded as in design D3. Verify with tests that the default URL is unchanged, that `sslmode=disable&connect_timeout=10` and `options=-c statement_timeout=5000` read back through `make_url`, and that a set `CURATE_D_LEDGER_DATABASE_URL` is not changed by set options: `uv run pytest tests/unit/test_config.py --no-cov`.

## 2. Stream B: libpq connections (`src/curate_d/runtime/lease.py`, `ops/reset/reset.py`, `tests/unit/runtime/test_lease.py`, new `tests/unit/infrastructure/test_reset_connect.py`)

- [x] 2.1 Add `libpq_conninfo` to `runtime/lease.py` as in design D4, and make `lease_conninfo` use it. Change the assertion at `tests/unit/runtime/test_lease.py:234` to compare the parsed conninfo. Verify with tests that a space in `options`, the credentials `u@x` and `p/w`, and `sslmode` all reach `conninfo_to_dict` unchanged, and that a repeated key raises an error that names it: `uv run pytest tests/unit/runtime/test_lease.py --no-cov`.
- [x] 2.2 Make `connect` in `ops/reset/reset.py` use `libpq_conninfo`. Verify with a test that loads `ops/reset/reset.py` through `importlib`, replaces `psycopg.connect`, and checks that it receives the helper's conninfo: `uv run pytest tests/unit/infrastructure/test_reset_connect.py --no-cov`.
- [x] 2.3 Add a test that builds the DBOS configuration from settings with options and reads `sslmode` back from its `system_database_url`. Verify with `uv run pytest tests/unit/runtime/test_dbos_app.py --no-cov`.

## 3. Stream C: documentation (`docs/staging.md`)

- [x] 3.1 Add `CURATE_D_LEDGER_DATABASE_OPTIONS` to the settings table, with the format, an example, the rejected keys, and the precedence of the full URL. Verify with `grep -n "LEDGER_DATABASE_OPTIONS" docs/staging.md`.

## 4. Integration check

- [x] 4.1 Run `make lint`, `make typecheck`, `uv run pytest -n auto tests/unit --no-cov` and `sonar analyze secrets src/curate_d/config.py src/curate_d/runtime/lease.py`, and verify that all pass.
- [x] 4.2 Run `make test-integration-parallel`, which starts a fresh stack per shard, and verify that it passes. The lease sessions now connect through `libpq_conninfo`.
