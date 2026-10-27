# Design

## Context

See proposal.md, Why. Three places name the local bucket and prefix today:

- the two RustFS policies, as ARNs (`arn:aws:s3:::curate-d-staging/content`, `…/content/*`,
  and for the writer `arn:aws:s3:::curate-d-staging`). `rc admin policy create` takes the
  files as they are, so they cannot be templated without generating them at init time;
- `compose.yaml`'s `x-staging-s3` block, which feeds both workers' `Settings`;
- `rustfs-init`'s `RUSTFS_STAGING_BUCKET`/`RUSTFS_STAGING_PREFIX`, interpolated from the
  same shell variables as the workers' values, with `initialize.sh` falling back to `content/`
  for a missing prefix.

The pinned `rustfs/rc` image ships BusyBox and `jq` (`/usr/bin/jq`).

## Goals / Non-Goals

**Goals:**
- Provisioning cannot create a bucket or prefix other than the one the policies grant,
  whatever the shell exports.
- A change to the bucket or prefix that misses either the policies or the workers' compose
  values fails `make check`, before anyone brings up a stack.

**Non-Goals:**
- Templating the policies so the local bucket becomes configurable again. The user ruled
  that out: the local stack uses one fixed bucket and prefix.
- Changing the integration fixtures' `CURATE_D_TEST_RUSTFS_BUCKET` override. It already
  defaults to `curate-d-staging` and selects which bucket the tests address, not what is
  provisioned.
- OVH: its buckets and prefixes stay per-environment settings.

## Decisions

### The policies are the source of truth; `initialize.sh` reads the bucket and prefix from them

`initialize.sh` derives `BUCKET` and `PREFIX` from the one `…/*` object grant in each policy:
it strips `arn:aws:s3:::` and the trailing `*`, splits at the first `/`, and requires both
policies to name the same grant. If a policy has no single prefix grant, or the two policies
disagree, it fails naming the file before it creates anything. `compose.yaml` stops passing
`RUSTFS_STAGING_BUCKET` and `RUSTFS_STAGING_PREFIX`.

The policies must name the bucket literally, so they are where it lives. Reading from them
makes the original failure, provisioning one bucket while granting another, impossible by
construction, even with `docker compose run -e RUSTFS_STAGING_BUCKET=… rustfs-init`.

Alternatives considered:
- **Keep the inputs and feed them from compose literals, sharing one YAML scalar anchor with
  the workers' values.** Init and workers could never disagree, but init and policies still
  could, if someone overrode the variable on `docker compose run` or edited one side. That is
  the failure being fixed, so this leaves the hole open.
- **Hard-code the constants in `initialize.sh`.** A third copy of the same two strings.

### The workers' compose values are literals, pinned to the policies by a unit test

`Settings` reads the bucket and prefix from the environment, so compose has to state them
for the workers. It does so as literals: `curate-d-staging`, `content/`. A new
`tests/unit/infrastructure/test_rustfs_provisioning.py` parses `compose.yaml` and the
policies and fails when:
- either worker's bucket or prefix holds an interpolation, or
- the policies grant anything other than `<bucket>`, `<bucket>/<prefix without slash>` and
  `<bucket>/<prefix>*`, or
- `rustfs-init` is given a bucket or prefix input again.

This is the second copy, kept because `Settings` needs it, and the test is what keeps it in
line. A unit test fails sooner than the worker's readiness check: `make check`, not
`make stack-up`.

### Region in the `rustfs` service comment

Compose sets `CURATE_D_STAGING_S3_REGION: us-east-1`. The OVH invocation in
`infrastructure/README.md` and `infrastructure/staging-compatibility.md` sign with
`us-east-va` (row 35). The `Settings` default is `US-EAST-VA`. The comment now names five
differences: endpoint, signing region, addressing style, TLS trust and credentials. That
matches `infrastructure/README.md`'s "Content staging on RustFS" paragraph.

## Risks / Trade-offs

- [`jq` leaves a future pinned `rc` image] → `rustfs-init` fails on its first run in CI,
  naming the missing command, so the image bump's PR cannot merge unnoticed.
- [A policy gains a second `…/*` grant (for example a second prefix)] → `initialize.sh`
  fails with a clear message, and the unit test fails first. Supporting more than one prefix
  is a design change, not a silent widening.
- [Developers exporting the old variables] → compose ignores them for the local stack. That
  setup never passed provisioning, so nothing that worked stops working.

## Migration Plan

None. Existing local volumes already hold `curate-d-staging`, and `rustfs-init` converges
on the next `make stack-up`.
