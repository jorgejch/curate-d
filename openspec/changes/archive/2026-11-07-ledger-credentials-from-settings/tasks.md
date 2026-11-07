# Tasks

This change has one work stream and cannot split further: groups 1 and 2 edit one field set in
`src/curate_d/config.py` and its tests. Group 3 touches only `docs/staging.md` and can start at any
time, because the variable names are fixed in the spec.

## 1. Settings (`src/curate_d/config.py`, `tests/unit/test_config.py`)

- [x] 1.1 Add `ledger_database_user` and `ledger_database_password` before `ledger_database_url`, with the default `curate_d` and `min_length=1`. Verify with a test that an empty password fails to load, names the field, and does not contain the input: `uv run pytest tests/unit/test_config.py --no-cov`.
- [x] 1.2 Add `_local_ledger_url`, which percent-encodes both credentials and builds the URL. Set it as the `default_factory` of `ledger_database_url`, and remove `_LOCAL_LEDGER_DSN`. Verify with tests that the default URL equals the previous default, and that `u@x` and `p/w:#?` read back through `make_url` with the host `localhost`: `uv run pytest tests/unit/test_config.py --no-cov`.
- [x] 1.3 Add a test that a set `CURATE_D_LEDGER_DATABASE_URL` takes precedence over a set password. The existing test `test_settings_are_read_from_the_prefixed_environment` stays. Verify with `uv run pytest tests/unit/test_config.py --no-cov`.

## 2. Secret scan

- [x] 2.1 Run `sonar analyze secrets src/curate_d/config.py` and verify that it reports no secret. Also verify that the `Read` tool can open `src/curate_d/config.py` with the Sonar hook active.

## 3. Documentation (`docs/staging.md`)

- [x] 3.1 Add `CURATE_D_LEDGER_DATABASE_USER` and `CURATE_D_LEDGER_DATABASE_PASSWORD` to the settings table. State that they apply only to the local default URL, and that `CURATE_D_LEDGER_DATABASE_URL` takes precedence. Verify that each name in the table matches a `Settings` field with `grep -n "ledger_database" src/curate_d/config.py`.

## 4. Integration check

- [x] 4.1 Run `make lint` and `uv run pytest -n auto tests/unit --no-cov`, and verify that both pass. Task 1.2 shows that the default URL does not change, so this change adds no integration test.

## 5. Host and port settings (`src/curate_d/config.py`, `tests/unit/test_config.py`, `docs/staging.md`)

- [x] 5.1 Add `ledger_database_host` and `ledger_database_port`, and rename `_local_ledger_url` to `_ledger_url`, which puts every URL part together. Verify with tests that a set host and port reach the URL, and that an invalid host or port stops settings load without the input in the error: `uv run pytest tests/unit/test_config.py --no-cov`.
- [x] 5.2 Extend the precedence test, so that a set `CURATE_D_LEDGER_DATABASE_URL` wins over all four parts. Verify with `uv run pytest tests/unit/test_config.py --no-cov`.
- [x] 5.3 Add the two variables to the settings table in `docs/staging.md`. Verify with `grep -n "LEDGER_DATABASE" docs/staging.md`.
- [x] 5.4 Run `make lint`, `make typecheck`, `uv run pytest -n auto tests/unit --no-cov` and `sonar analyze secrets src/curate_d/config.py`, and verify that all pass.

## 6. Database name setting (`src/curate_d/config.py`, `tests/unit/test_config.py`, `docs/staging.md`)

- [x] 6.1 Add `ledger_database_name` and pass it to `_ledger_url` as the URL path. Verify with tests that a set name reaches the URL, that an invalid name stops settings load without the input in the error, and that a set `CURATE_D_LEDGER_DATABASE_URL` wins over the name: `uv run pytest tests/unit/test_config.py --no-cov`.
- [x] 6.2 Add the variable to the settings table in `docs/staging.md`. Verify with `grep -n "LEDGER_DATABASE" docs/staging.md`.
- [x] 6.3 Run `make lint`, `make typecheck`, `uv run pytest -n auto tests/unit --no-cov` and `sonar analyze secrets src/curate_d/config.py`, and verify that all pass.
