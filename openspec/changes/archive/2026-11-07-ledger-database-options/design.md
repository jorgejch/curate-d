# Design

## Context

`_ledger_url` (`src/curate_d/config.py`) builds `ledger_database_url` from the five part settings
with `PostgresDsn.build`, when `CURATE_D_LEDGER_DATABASE_URL` is unset. It is the
`default_factory` of the URL field, so it reads only fields declared before that field.

Readers of the URL fall into two groups:

- `runtime/dbos_app.py`, `runtime/datasource.py`, `runtime/retention.py`, `api/app.py`,
  `staging/factories.py`, `configuration/verify.py` and `migrations/env.py` give the URL string to
  SQLAlchemy or DBOS unchanged. SQLAlchemy decodes the query and passes it to psycopg as keyword
  arguments.
- `runtime/lease.py` (`lease_conninfo`, used by the lease sessions, the schedule authority,
  promotion and the version lease) and `ops/reset/reset.py` (`connect`) call
  `make_url(...).set(drivername="postgresql").render_as_string(...)` and give the result to libpq.

A prototype in the scratchpad showed that `render_as_string` writes a space in a query value as
`+`. `psycopg.conninfo.conninfo_to_dict` then reads `options=-c+x` as `-c+x`: libpq does not decode
`+`.

## Goals / Non-Goals

**Goals:**

- One setting for libpq connection parameters, used only when the part settings build the URL.
- The same parameter values for every reader, SQLAlchemy and libpq alike.

**Non-Goals:**

- Checking parameter names against the libpq keyword list. An unknown key fails when the first
  connection opens, which is at startup.
- A parameter that appears twice with intent, such as a list of hosts. A repeated key is rejected.

## Decisions

### D1: A query string, not JSON

The value is `key=value&key=value`, the form an operator already sees in a URL. pydantic-settings
reads a `dict` field only as JSON, which is harder to write in a manifest and gives no gain here.

### D2: Validate in a field validator

`ledger_database_options` is a `str` with the default `""`, declared before
`ledger_database_url`. A `field_validator` parses it with
`urllib.parse.parse_qsl(value, strict_parsing=True)` and rejects:

- a malformed value (`sslmode`, `a=1&&b=2`), which `strict_parsing` raises on;
- an empty key (`=x`), which `parse_qsl` accepts;
- a repeated key;
- a key, compared without case, from `user`, `password`, `passfile`, `host`, `hostaddr`, `port`,
  `dbname` and `service`. Each of these sets a part of the connection that a part setting owns, and
  `password` or `passfile` would put a secret in a plain field.

The error message names the rejected key, never a value. `_hide_input_in_errors` then removes the
input from the error, as it does for every field.

### D3: Encode with `%20`

`_ledger_url` passes `urlencode(pairs, quote_via=quote)` as the `query` of `PostgresDsn.build`.
`quote` writes a space as `%20`, which SQLAlchemy and libpq both decode to a space. The default
`quote_plus` writes `+`, which libpq does not decode. An empty value gives no query, so the default
URL does not change.

### D4: Build libpq connection information from the parsed URL

A new helper in `runtime/lease.py`, `libpq_conninfo(url: str) -> str`, parses the URL with
`make_url`, maps its parts with `translate_connect_args(username="user", database="dbname")`, adds
each query parameter, and returns `psycopg.conninfo.make_conninfo(...)`. The prototype showed that
this keeps `u@x`, `p/w` and `-c statement_timeout=5000` exactly. A repeated key reaches the helper
only through a full URL, as a tuple; the helper raises `ValueError` that names the key.

`lease_conninfo` and `ops/reset/reset.py`'s `connect` both call the helper. `lease_conninfo` then
returns a keyword string (`host=... dbname=...`), not a URL. Every caller gives that string to
`psycopg.connect`, which accepts both forms. `tests/unit/runtime/test_lease.py:234` compares the
string to a URL, so that test compares the parsed result instead.

Alternative considered: keep the URL form and write the query by hand after
`url.set(query={})`. It duplicates the encoding rules of D3 in a second place, and the keyword form
has no encoding to get wrong.

## Risks / Trade-offs

- [DBOS receives the system database URL with a query. I did not test whether DBOS keeps it.] →
  The integration suite on a fresh `make test-stack` runs with the default URL, which has no query.
  A task adds a check that builds the DBOS configuration with options and reads them back. A run
  against a real server with `sslmode` is not part of this change.
- [The options are visible in the settings `repr` and in logs.] → The rejected keys keep the
  password and the password file out. A parameter such as `sslkey` names a file, not a secret.
- [`lease_conninfo` changes its return form.] → Every caller passes it to `psycopg.connect`, which
  accepts both, and the integration suite exercises the lease sessions.
