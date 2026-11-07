# Design

## Context

`Settings` (`src/curate_d/config.py`) is a frozen `BaseSettings` with the prefix `CURATE_D_`.
`ledger_database_url` is a `PostgresDsn` with a literal default. All readers use that attribute:
`runtime/dbos_app.py`, `runtime/datasource.py`, `api/app.py`, `staging/factories.py`,
`configuration/verify.py`, `migrations/env.py`, `ops/reset/reset.py` and the tests.

The pinned versions are `pydantic` 2.14.2 and `pydantic-settings` 2.16.2.

## Goals / Non-Goals

**Goals:**

- Remove the credential URL from `config.py`, so that the Sonar scan of the file is clean.
- Keep the current default URL, character for character.
- Keep the attribute name and the type for every reader.

**Non-Goals:**

- `compose.yaml` and `ops/README.md`. They set the full URL and continue to work. The scan
  reports `compose.yaml:30` and `compose.yaml:42`, and these findings stay.
- Hiding the password in the `repr` of `ledger_database_url`. The URL type shows it today.

## Decisions

### D1: Derive the default with a `default_factory` that takes validated data

`ledger_database_url` uses `Field(default_factory=_ledger_url)`. The factory receives the fields
that pydantic already validated. The five new fields must therefore come before the URL field in
the class.

A prototype in the scratchpad confirmed each point:

- With no variable set, the URL is identical to the current default, and its type is `PostgresDsn`.
- A set `CURATE_D_LEDGER_DATABASE_URL` means that pydantic does not call the factory.
- `sonar analyze secrets` reports no finding on the prototype, which has the
  `SecretStr("curate_d")` default.

Alternatives considered:

- A `@computed_field` or a property. This changes how `CURATE_D_LEDGER_DATABASE_URL` reaches the
  attribute, because a property is not a settings source.
- A `mode="before"` model validator. It must read the merged source data and apply the field
  defaults by hand, and it duplicates what the factory receives.
- `PostgresDsn | None` with a later fill-in. Every reader then has to handle `None`.

### D2: Percent-encode the credentials before the build

`PostgresDsn.build` does not encode `username` or `password`. In the prototype, the user `u@x`
with the password `p/w:#?` raised `invalid port number`. The factory therefore applies
`urllib.parse.quote(value, safe="")` to both values. SQLAlchemy's `make_url` then read back `u@x`
and `p/w:#?`. `curate_d` has no reserved character, so encoding does not change the default.

### D3: Reject an empty credential

Both fields use `min_length=1`. When a credential fails, pydantic reports
`default_factory_not_called` for the URL field as well. `_hide_input_in_errors` can rebuild that
error type: the prototype confirmed this with `ValidationError.from_exception_data`. A test covers
the complete path.

### D4: Keep the unset defaults local

With no variable set, the URL points at `localhost:5433`. A process reaches a remote database only
when an operator sets the host, so the statement in the module docstring stays true. The docstring
does not change.

### D5: Constrain the host, the port and the database name

`PostgresDsn.build` does not encode the host. A host such as `db/other` would move text into the
URL path, and `a,b` would make a multi-host URL, in both cases with no error. The host field
therefore has a pattern that accepts one name, one IPv4 address or one bracketed IPv6 address. The
port field has the range 1 to 65535. The database name field accepts only letters, digits and
`_`, `.`, `$`, `-`, because the URL builder does not encode the path and `?` would start the URL
query. A database with another kind of name needs the full URL. An invalid part stops settings load even when the full URL is
set, because a wrong value in the environment is an operator error in both cases.

## Risks / Trade-offs

- [The part settings apply only to the derived URL. A process that sets the full URL and also a
  part uses the URL, with no warning.] → The precedence is a requirement, a test covers it, and
  `docs/staging.md` states it.
- [A deployment that sets a host but not the credentials sends the default credentials to that
  host.] → The connection fails at startup on any database with other credentials, and
  `docs/staging.md` states that a remote database needs all five parts.
- [SonarQube Cloud can report `SecretStr("curate_d")` under a rule that the local secrets scan does
  not run, such as a Python hardcoded-credential rule. The SonarQube MCP server did not connect in
  this session, so this is not confirmed.] → Check the pull request's Sonar result, and record the
  outcome in the review.
- [Field order is now significant. A later reorder makes the factory raise `KeyError`.] → The unit
  tests load the default on every run, so a reorder fails the build.

## Migration Plan

No migration. Existing deployments set `CURATE_D_LEDGER_DATABASE_URL` or use the local default, and
both give the same URL as before. To roll back, revert the commit.
