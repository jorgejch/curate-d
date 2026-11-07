# Proposal

## Why

`src/curate_d/config.py:32` holds a complete PostgreSQL URL with a password in it. Sonar reports
this as `secrets:S6698`, and the Sonar `Read` hook therefore refuses every agent read of the
file. The ledger credentials also cannot change unless the operator replaces the whole URL.

## What Changes

- Add the setting `ledger_database_user`, read from `CURATE_D_LEDGER_DATABASE_USER`. The default
  is `curate_d`.
- Add the setting `ledger_database_password`, read from `CURATE_D_LEDGER_DATABASE_PASSWORD`. The
  default is `curate_d`.
- Add the setting `ledger_database_host`, read from `CURATE_D_LEDGER_DATABASE_HOST`. The default
  is `localhost`.
- Add the setting `ledger_database_port`, read from `CURATE_D_LEDGER_DATABASE_PORT`. The default
  is `5433`.
- Add the setting `ledger_database_name`, read from `CURATE_D_LEDGER_DATABASE_NAME`. The default
  is `curate_d`.
- Derive the default `ledger_database_url` from these five settings, for a local or a remote
  database.
- Remove `_LOCAL_LEDGER_DSN`, so that `config.py` holds no credential URL.
- Keep `CURATE_D_LEDGER_DATABASE_URL`. When it is set, it takes precedence over the new settings.
- No breaking change: with no new variable set, the URL is identical to the current default.

## Capabilities

### New Capabilities

- `ledger-connection`: how a process resolves the address and the credentials of the ledger
  database from its settings.

### Modified Capabilities

None. No existing spec states how the ledger URL resolves.

## Impact

- `src/curate_d/config.py`: five new fields, one new default factory, one constant removed.
- `tests/unit/test_config.py`: new tests.
- `docs/staging.md`: five new rows in the settings table.
- The readers of `settings.ledger_database_url` do not change. The attribute and its
  `PostgresDsn` type stay.
- `compose.yaml` stays out of scope. Lines 30 and 42 still hold findings (`secrets:S6698`,
  `secrets:S2068`), and the Sonar hook will continue to refuse reads of that file.
