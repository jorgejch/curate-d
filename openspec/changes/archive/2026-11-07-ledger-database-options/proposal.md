# Proposal

## Why

The part settings (`CURATE_D_LEDGER_DATABASE_USER`, `_PASSWORD`, `_HOST`, `_PORT`, `_NAME`) cannot
set connection parameters such as `sslmode=disable`. An operator who needs one must give up the
part settings and set the full `CURATE_D_LEDGER_DATABASE_URL`.

Two readers of the URL also change a parameter value on the way to libpq. `runtime/lease.py` and
`ops/reset/reset.py` render the URL again through SQLAlchemy, which writes a space as `+`. libpq
does not decode `+`, so `options=-c statement_timeout=5000` reaches PostgreSQL as
`-c+statement_timeout=5000`. This fault already exists for a full URL, and the new setting would
make it easy to trigger.

## What Changes

- Add the setting `ledger_database_options`, read from `CURATE_D_LEDGER_DATABASE_OPTIONS`. Its
  value is a URL query string, such as `sslmode=disable&connect_timeout=10`. The default is empty.
- Add the options to the URL that the part settings build. With no options, the URL is identical
  to the current one.
- Reject malformed options, a repeated key, and a key that names a part of the connection (such as
  `password` or `host`), so that a secret cannot sit in a plain setting.
- Keep `CURATE_D_LEDGER_DATABASE_URL`. When it is set, the options do not change it.
- Make the libpq connections of the lease sessions, promotion and environment reset keep every
  URL parameter with its exact value. `lease_conninfo` then returns a keyword conninfo string,
  not a URL.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ledger-connection`: adds the options setting, extends the precedence requirement to the
  options, and requires direct connections to keep the URL parameters.

## Impact

- `src/curate_d/config.py`: one new field and its validator; `_ledger_url` adds the query.
- `src/curate_d/runtime/lease.py`: a new helper that turns a URL into a libpq conninfo string;
  `lease_conninfo` uses it.
- `ops/reset/reset.py`: `connect` uses the same helper.
- `tests/unit/test_config.py`, `tests/unit/runtime/test_lease.py`: new and updated tests. A new
  test covers `ops/reset/reset.py`.
- `docs/staging.md`: one new row in the settings table.
- The other readers pass the URL string unchanged, so they do not change: `runtime/dbos_app.py`,
  `runtime/datasource.py`, `runtime/retention.py`, `api/app.py`, `staging/factories.py`,
  `configuration/verify.py` and `migrations/env.py`.
