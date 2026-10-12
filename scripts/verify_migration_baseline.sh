#!/usr/bin/env bash
# One-off check for issue #7: the squashed baseline builds the same schema as the old chain.
#
# Builds two throwaway databases on the local stack's PostgreSQL: one with the migration
# chain at <pre-squash-ref> (run from a worktree of that commit, since its migrations import
# from that tree's src), one with this checkout's migrations. Then diffs
# `pg_dump --schema-only` of both, `alembic_version` excluded, and exits non-zero on any
# difference. Both get the DBOS system schema from the same DBOS version, so `dbos.*` is
# compared too and must match.
#
# Not part of `make check` or CI; drop it once #7 is closed.
#
# Usage: scripts/verify_migration_baseline.sh <pre-squash-ref> [output-dir]
#
# Environment:
#   VERIFY_POSTGRES_CONTAINER  the stack's PostgreSQL container (default curate-d-postgres-1)
#   VERIFY_POSTGRES_URL        server URL reachable from this machine, no database
#                              (default postgresql+psycopg://curate_d:curate_d@localhost:5433)
#   VERIFY_POSTGRES_USER       role for psql and pg_dump in the container (default curate_d)
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <pre-squash-ref> [output-dir]" >&2
    exit 2
fi

ref=$1
container=${VERIFY_POSTGRES_CONTAINER:-curate-d-postgres-1}
server_url=${VERIFY_POSTGRES_URL:-postgresql+psycopg://curate_d:curate_d@localhost:5433}
role=${VERIFY_POSTGRES_USER:-curate_d}

repo_root=$(git rev-parse --show-toplevel)
commit=$(git -C "$repo_root" rev-parse --verify "$ref^{commit}")
scratch=$(mktemp -d)
out=${2:-$scratch}
mkdir -p "$out"
suffix=$(date +%s)$$
old_db="curate_d_baseline_old_$suffix"
new_db="curate_d_baseline_new_$suffix"
old_tree="$scratch/pre-squash"

psql_admin() {
    docker exec "$container" psql -v ON_ERROR_STOP=1 -U "$role" -d postgres -qtAc "$1"
}

cleanup() {
    psql_admin "DROP DATABASE IF EXISTS \"$old_db\" WITH (FORCE)" >/dev/null 2>&1 || true
    psql_admin "DROP DATABASE IF EXISTS \"$new_db\" WITH (FORCE)" >/dev/null 2>&1 || true
    git -C "$repo_root" worktree remove --force "$old_tree" >/dev/null 2>&1 || true
}
trap cleanup EXIT

migrate() {
    # $1: tree to run Alembic from, $2: database name.
    (
        cd "$1"
        CURATE_D_LEDGER_DATABASE_URL="$server_url/$2" uv run --frozen --quiet alembic upgrade head
    )
}

head_of() {
    docker exec "$container" psql -U "$role" -d "$1" -qtAc 'SELECT version_num FROM alembic_version'
}

dump() {
    # pg_dump 17.6+/18 brackets its output with \restrict lines carrying a random key.
    docker exec "$container" pg_dump -U "$role" -d "$1" --schema-only --no-owner \
        --exclude-table=public.alembic_version | grep -Ev '^\\(un)?restrict '
}

echo "pre-squash chain: $commit"
echo "baseline:         $(git -C "$repo_root" rev-parse HEAD) (plus any uncommitted changes)"

psql_admin "CREATE DATABASE \"$old_db\""
psql_admin "CREATE DATABASE \"$new_db\""

git -C "$repo_root" worktree add --detach --quiet "$old_tree" "$commit"
migrate "$old_tree" "$old_db"
migrate "$repo_root" "$new_db"

echo "old chain head: $(head_of "$old_db")"
echo "baseline head:  $(head_of "$new_db")"

dump "$old_db" >"$out/old-chain.sql"
dump "$new_db" >"$out/baseline.sql"

# Objects the issue names, so an empty diff cannot come from both dumps missing them.
expected=(
    'CONSTRAINT ck_audit_entries_ck_audit_entries_entry_type_known CHECK'
    "'workflow_command_completed'::text"
    'ADD CONSTRAINT uq_placements_destination_name_locator UNIQUE (destination_name, locator)'
    "CREATE UNIQUE INDEX uq_audit_entries_command_request_id ON public.audit_entries USING btree (((details ->> 'request_id'::text))) WHERE (entry_type = 'workflow_command_issued'::text)"
    'CREATE INDEX ix_dry_run_items_run_id_destination_name_locator'
    'CREATE FUNCTION public.curate_d_reject_audit_mutation()'
    'CREATE TRIGGER audit_entries_append_only BEFORE DELETE OR UPDATE'
    'CREATE TRIGGER audit_entries_no_truncate BEFORE TRUNCATE'
    'CREATE VIEW public.workflow_status_by_queue AS'
    'CREATE VIEW public.stalled_workflows AS'
    'CREATE VIEW public.failed_workflows AS'
    'LEFT JOIN dbos.workflow_output output'
    "COMMENT ON COLUMN public.placements.attempts IS 'Attempts after the first"
)
missing=0
for needle in "${expected[@]}"; do
    for file in old-chain.sql baseline.sql; do
        if ! grep -qF -- "$needle" "$out/$file"; then
            echo "missing from $file: $needle" >&2
            missing=1
        fi
    done
done
echo "named objects present in both dumps: $([[ $missing -eq 0 ]] && echo yes || echo NO)"
echo "comments: $(grep -c '^COMMENT ON' "$out/old-chain.sql") in the old chain," \
    "$(grep -c '^COMMENT ON' "$out/baseline.sql") in the baseline"

echo "--- diff -u old-chain.sql baseline.sql (dumps in $out)"
if diff -u "$out/old-chain.sql" "$out/baseline.sql"; then
    echo "--- empty diff"
    exit "$missing"
fi
exit 1
