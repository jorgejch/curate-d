"""The database and bucket half of `reset-environment.sh`, run inside the curate.d image.

`reset-environment.sh` passes this file's source to `python -c` in a container built from
the I/O worker's definition (locally `docker compose run`, in dev a one-off pod from its
StatefulSet), so it resolves `Settings` exactly as that worker does: its ledger URL and
the staging writer identity. Nothing here is installed in the image or imported by the app.

    plan    <env>                     Print what a reset would remove. Changes nothing.
    wipe    <env> [--drop-connections]
                                      Empty the ledger and the content prefix.
    restore <env>                     Put kept provider connections back after migrating.

The script runs `wipe`, then `alembic upgrade head`, then `restore`, in one container.
"""

import asyncio
import sys
from typing import Final, LiteralString

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from curate_d.config import DeploymentEnvironment, Settings
from curate_d.staging.factories import refresh_readiness_marker
from curate_d.staging.object_store import build_client_factory

# Provider connections wait here, outside `public`, while the ledger is rebuilt. A stash
# left by a run that failed before `restore` is kept and restored by the next run.
STASH: Final = "curate_d_reset"
ALLOWED: Final = {DeploymentEnvironment.LOCAL, DeploymentEnvironment.DEV}

_COUNTS: Final = (
    "public.provider_connections",
    "public.audit_entries",
    "public.source_objects",
    "public.placements",
    "dbos.workflow_status",
    "dbos.workflow_schedules",
)

# Every query skips extension members (`pg_depend.deptype = 'e'`), so PostGIS's
# `spatial_ref_sys`, types and functions stay.
_DROP_PUBLIC: Final = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname, c.relkind FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
      AND c.relowner = current_user::regrole AND NOT c.relispartition
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('DROP %s IF EXISTS public.%I CASCADE',
      CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
        WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
      r.relname);
  END LOOP;
  FOR r IN
    SELECT p.oid::regprocedure::text AS signature, p.prokind FROM pg_proc p
    WHERE p.pronamespace = 'public'::regnamespace AND p.proowner = current_user::regrole
      AND p.prokind IN ('f', 'p', 'a')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('DROP %s IF EXISTS %s CASCADE',
      CASE r.prokind WHEN 'a' THEN 'AGGREGATE' ELSE 'ROUTINE' END, r.signature);
  END LOOP;
  FOR r IN
    SELECT t.typname, t.typtype FROM pg_type t
    LEFT JOIN pg_class c ON c.oid = t.typrelid
    WHERE t.typnamespace = 'public'::regnamespace AND t.typowner = current_user::regrole
      AND (t.typtype IN ('e', 'd', 'r', 'm') OR (t.typtype = 'c' AND c.relkind = 'c'))
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('DROP %s IF EXISTS public.%I CASCADE',
      CASE r.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, r.typname);
  END LOOP;
END $$
"""

_SURVIVORS: Final = """
SELECT string_agg(c.relname, ', ') FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
"""


def say(message: str) -> None:
    """Write one line of the plan or progress to stdout."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def settings_for(environment: str) -> Settings:
    """Return the settings, refusing any environment but local or dev, or a mismatch."""
    settings = Settings()
    configured = settings.environment.value if settings.environment else "no environment"
    if settings.environment not in ALLOWED or configured != environment:
        raise SystemExit(
            f"refused: asked to reset {environment}, but this container is configured for "
            f"{configured}; only local and dev can be reset"
        )
    return settings


def connect(settings: Settings) -> psycopg.Connection:
    """Open an autocommit connection to the ledger as the application role."""
    url = make_url(str(settings.ledger_database_url)).set(drivername="postgresql")
    return psycopg.connect(
        url.render_as_string(hide_password=False),
        autocommit=True,
        application_name="curate-d environment reset",
    )


def table_exists(connection: psycopg.Connection, name: str) -> bool:
    """Return whether `name` (schema-qualified) is an existing table."""
    row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


def scalar(connection: psycopg.Connection, query: LiteralString) -> object:
    """Return the first column of the first row of `query`."""
    row = connection.execute(query).fetchone()
    return None if row is None else row[0]


async def bucket_contents(settings: Settings) -> tuple[list[str], list[tuple[str, str]]]:
    """Return the object keys under the content prefix (marker excluded) and open uploads."""
    bucket, prefix = settings.staging_s3_bucket, settings.staging_s3_content_prefix
    keys: list[str] = []
    uploads: list[tuple[str, str]] = []
    async with build_client_factory(settings)() as client:
        async for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            keys += [item["Key"] for item in page.get("Contents", []) if item["Key"] != prefix]
        async for upload_page in client.get_paginator("list_multipart_uploads").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            uploads += [(item["Key"], item["UploadId"]) for item in upload_page.get("Uploads", [])]
    return keys, uploads


async def empty_bucket(settings: Settings) -> None:
    """Delete every object under the content prefix, abort its uploads, keep the marker."""
    bucket, prefix = settings.staging_s3_bucket, settings.staging_s3_content_prefix
    keys, uploads = await bucket_contents(settings)
    async with build_client_factory(settings)() as client:
        # One DeleteObject per key: DeleteObjects needs a body checksum header that not
        # every S3 implementation accepts, and dev's prefix is small.
        for key in keys:
            await client.delete_object(Bucket=bucket, Key=key)
        for key, upload_id in uploads:
            await client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    left, open_uploads = await bucket_contents(settings)
    if left or open_uploads:
        raise SystemExit(f"bucket not empty: {len(left)} objects, {len(open_uploads)} uploads")
    # The I/O worker would recreate it, but CPU workers' readiness reads it first.
    await refresh_readiness_marker(settings)
    say(f"bucket: deleted {len(keys)} objects, aborted {len(uploads)} uploads under {prefix}")


def plan(settings: Settings) -> None:
    """Print the database, rows and bucket objects a reset would remove."""
    url = make_url(str(settings.ledger_database_url))
    say(f"database: {url.database} on {url.host}:{url.port} as {url.username}")
    with connect(settings) as connection:
        for table in _COUNTS:
            if table_exists(connection, table):
                schema, name = table.split(".")
                query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, name))
                count = connection.execute(query).fetchone()
                say(f"  {table}: {count[0] if count else 0} rows")
        if table_exists(connection, f"{STASH}.provider_connections"):
            say(f"  {STASH}.provider_connections: kept from an earlier failed run")
    keys, uploads = asyncio.run(bucket_contents(settings))
    say(
        f"bucket: {settings.staging_s3_bucket}/{settings.staging_s3_content_prefix} "
        f"holds {len(keys)} objects and {len(uploads)} open uploads"
    )


def wipe(settings: Settings, *, keep_connections: bool) -> None:
    """Empty the ledger (public and the DBOS schema), then the content prefix."""
    with connect(settings) as connection:
        leases = scalar(
            connection,
            "SELECT string_agg(application_name, ', ') FROM pg_stat_activity "
            "WHERE datname = current_database() AND application_name LIKE 'curate-d executor%'",
        )
        if leases:
            raise SystemExit(f"refused: workers still hold their leases ({leases}); stop them")
        # Pooled server connections and anything else of the application's.
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND usename = current_user "
            "AND pid <> pg_backend_pid()"
        )
        with connection.transaction():
            if not keep_connections:
                connection.execute("DROP SCHEMA IF EXISTS curate_d_reset CASCADE")
            elif not table_exists(connection, f"{STASH}.provider_connections"):
                connection.execute("CREATE SCHEMA IF NOT EXISTS curate_d_reset")
                if table_exists(connection, "public.provider_connections"):
                    connection.execute(
                        "CREATE TABLE curate_d_reset.provider_connections "
                        "AS TABLE public.provider_connections"
                    )
            connection.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
            connection.execute(_DROP_PUBLIC)
            survivors = scalar(connection, _SURVIVORS)
            if survivors:
                raise SystemExit(f"refused: tables owned by another role remain: {survivors}")
        say("ledger: emptied public and dropped the dbos schema")
    asyncio.run(empty_bucket(settings))


def restore(settings: Settings) -> None:
    """Copy kept provider connections back into the migrated ledger and drop the stash."""
    with connect(settings) as connection, connection.transaction():
        if not table_exists(connection, f"{STASH}.provider_connections"):
            say("connections: none kept")
            connection.execute("DROP SCHEMA IF EXISTS curate_d_reset CASCADE")
            return
        # Only the columns both tables have, in case the stash predates a migration.
        columns = scalar(
            connection,
            "SELECT string_agg(quote_ident(column_name), ', ') FROM information_schema.columns "
            "WHERE table_schema = 'curate_d_reset' AND table_name = 'provider_connections' "
            "AND column_name IN (SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'provider_connections')",
        )
        names = sql.SQL(str(columns))
        restored = connection.execute(
            sql.SQL(
                "INSERT INTO public.provider_connections ({names}) "
                "SELECT {names} FROM curate_d_reset.provider_connections ON CONFLICT DO NOTHING"
            ).format(names=names)
        ).rowcount
        connection.execute("DROP SCHEMA curate_d_reset CASCADE")
        say(f"connections: restored {restored}")


def main(arguments: list[str]) -> None:
    """Dispatch `plan`, `wipe` or `restore` for the named environment."""
    if len(arguments) < 2 or arguments[0] not in {"plan", "wipe", "restore"}:
        raise SystemExit("usage: reset.py plan|wipe|restore <local|dev> [--drop-connections]")
    action, environment, *flags = arguments
    settings = settings_for(environment)
    if action == "plan":
        plan(settings)
    elif action == "wipe":
        wipe(settings, keep_connections="--drop-connections" not in flags)
    else:
        restore(settings)


if __name__ == "__main__":
    main(sys.argv[1:])
