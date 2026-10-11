#!/bin/sh
# Provision the local RustFS staging bucket, its two scoped identities, encryption, and
# cleanup — then prove each one rather than assume it.
#
# Idempotent: every step either creates or converges, so `make stack-up` on an existing
# volume is a no-op. Fails visibly instead of degrading: a silently weakened local
# guarantee is the one that gets discovered in the cluster.
#
# Mechanisms here are RustFS's and are deliberately NOT evidence about OVH. RustFS 1.0.0
# accepts and enforces the same AbortIncompleteMultipartUpload rule OVH carries, so both
# rules are declared below; `make lifecycle-check` observes them reclaim (rows 30/31).

set -eu

BUCKET="${RUSTFS_STAGING_BUCKET:?bucket name is required}"
PREFIX="${RUSTFS_STAGING_PREFIX:-content/}"
ENDPOINT="${RUSTFS_ENDPOINT:?endpoint is required}"
ROOT_USER="${RUSTFS_ROOT_USER:?root user is required}"
ROOT_PASSWORD="${RUSTFS_ROOT_PASSWORD:?root password is required}"
READER_KEY="${RUSTFS_STAGING_READER_KEY:?reader access key is required}"
READER_SECRET="${RUSTFS_STAGING_READER_SECRET:?reader secret is required}"
WRITER_KEY="${RUSTFS_STAGING_WRITER_KEY:?writer access key is required}"
WRITER_SECRET="${RUSTFS_STAGING_WRITER_SECRET:?writer secret is required}"
EXPIRE_DAYS="${RUSTFS_STAGING_EXPIRE_DAYS:-7}"
ABORT_DAYS="${RUSTFS_STAGING_ABORT_DAYS:-1}"

say() { echo "rustfs-init: $*"; }
fail() { echo "rustfs-init: FAILED: $*" >&2; exit 1; }

say "waiting for ${ENDPOINT}"
rc alias set admin "$ENDPOINT" "$ROOT_USER" "$ROOT_PASSWORD" >/dev/null \
    || fail "could not configure the admin alias"
i=0
until rc ready admin >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -ge 60 ] && fail "RustFS did not become ready at ${ENDPOINT}"
    sleep 1
done

say "bucket ${BUCKET}"
rc bucket create --ignore-existing "admin/${BUCKET}" >/dev/null \
    || fail "could not create ${BUCKET}"

# Setting the bucket default succeeds even when the server has no key to honour it; RustFS
# only refuses at write time. The encrypted probe write in the access matrix below is what
# proves SSE-S3 actually works.
say "server-side encryption"
rc bucket encryption set --mode sse-s3 "admin/${BUCKET}" >/dev/null \
    || fail "could not set SSE-S3 as the default on ${BUCKET}"
case "$(rc bucket encryption info --json "admin/${BUCKET}" 2>/dev/null)" in
    *'"mode": "SSE-S3"'*) ;;
    *) fail "SSE-S3 is not reported as the default on ${BUCKET}" ;;
esac

# Import replaces the whole lifecycle document, so the bucket converges on exactly these two
# rules on every run; an unrelated rule cannot stand in for a missing one.
say "lifecycle: expire after ${EXPIRE_DAYS}d, abort incomplete uploads after ${ABORT_DAYS}d"
lifecycle=/tmp/curate-d-lifecycle.json
cat > "$lifecycle" <<EOF
{
  "Rules": [
    {
      "ID": "expire-staged-content",
      "Status": "Enabled",
      "Filter": {"Prefix": "${PREFIX}"},
      "Expiration": {"Days": ${EXPIRE_DAYS}}
    },
    {
      "ID": "abort-incomplete-multipart-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": "${PREFIX}"},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": ${ABORT_DAYS}}
    }
  ]
}
EOF
rc bucket lifecycle rule import "admin/${BUCKET}" "$lifecycle" >/dev/null \
    || fail "could not apply the lifecycle rules"
rules="$(rc bucket lifecycle rule list --json "admin/${BUCKET}" 2>/dev/null || true)"
for rule in expire-staged-content abort-incomplete-multipart-uploads; do
    case "$rules" in
        *"$rule"*) ;;
        *) fail "lifecycle rule ${rule} is not stored on ${BUCKET}" ;;
    esac
done
rm -f "$lifecycle"

say "identities"
rc admin user add admin "$READER_KEY" "$READER_SECRET" >/dev/null \
    || fail "could not create the reader identity"
rc admin user add admin "$WRITER_KEY" "$WRITER_SECRET" >/dev/null \
    || fail "could not create the writer identity"

# Both policies also grant `<bucket>/content`, without the slash. RustFS evaluates a policy
# against the object key with its trailing slash removed, so the zero-byte readiness marker
# at the key `content/` is checked as `content` and matches neither `content/*` nor an
# explicit `content/`. The grant widens each identity by exactly that one key, which the
# application never writes; `test_neighbouring_keys_stay_denied` pins that nothing else
# outside the prefix is reachable. OVH needs no such grant.
rc admin policy create admin curate-d-staging-reader /policies/staging-reader.json >/dev/null \
    || fail "could not create the reader policy"
rc admin policy create admin curate-d-staging-writer /policies/staging-writer.json >/dev/null \
    || fail "could not create the writer policy"

rc admin policy attach admin curate-d-staging-reader --user "$READER_KEY" >/dev/null \
    || fail "could not attach the reader policy"
rc admin policy attach admin curate-d-staging-writer --user "$WRITER_KEY" >/dev/null \
    || fail "could not attach the writer policy"

# Prove the access matrix with the real identities. Provisioning that "succeeded" while
# leaving the reader able to write is the failure this exists to catch.
say "verifying access matrix and encryption"
rc alias set writer "$ENDPOINT" "$WRITER_KEY" "$WRITER_SECRET" >/dev/null
rc alias set reader "$ENDPOINT" "$READER_KEY" "$READER_SECRET" >/dev/null

probe="/tmp/curate-d-init-probe"
echo "curate-d init probe" > "$probe"
key="${PREFIX}.init-probe"

# Each denial is asserted with `if ... then fail`, not `cmd && fail`: the latter makes
# the whole list non-zero on the expected failure, which `set -e` would treat as the
# script itself failing.
rc put "$probe" "writer/${BUCKET}/${key}" >/dev/null 2>&1 \
    || fail "writer cannot upload; if the server rejects SSE-S3, is RUSTFS_SSE_S3_MASTER_KEY set?"
# `rc object stat` omits the SSE header from its output; its trace log carries the raw
# response headers. A format change there fails this check loudly, never passes it.
case "$(RUST_LOG=trace rc --debug object stat "writer/${BUCKET}/${key}" 2>&1)" in
    *'"x-amz-server-side-encryption": "AES256"'*) ;;
    *) fail "the probe object is not reported as SSE-S3 encrypted" ;;
esac
rc object show "reader/${BUCKET}/${key}" >/dev/null 2>&1 || fail "reader cannot read staged media"
if rc put "$probe" "reader/${BUCKET}/${PREFIX}.denied" >/dev/null 2>&1; then
    fail "reader can write"
fi
if rc object remove "reader/${BUCKET}/${key}" >/dev/null 2>&1; then
    fail "reader can delete"
fi
if rc object list "reader/${BUCKET}/" >/dev/null 2>&1; then
    fail "reader can list the bucket"
fi
if rc object show "reader/${BUCKET}/outside/object" >/dev/null 2>&1; then
    fail "reader escaped its prefix"
fi
if rc bucket create "writer/curate-d-init-denied" >/dev/null 2>&1; then
    rc bucket remove "admin/curate-d-init-denied" >/dev/null 2>&1 || true
    fail "writer can create buckets"
fi
rc object remove "writer/${BUCKET}/${key}" >/dev/null 2>&1 || fail "writer cannot delete"
rm -f "$probe"

say "ready: ${BUCKET} encrypted, reader read-only on ${PREFIX}, writer scoped to it"
