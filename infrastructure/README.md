# Infrastructure

This repository holds the **container build**; the Kubernetes manifests live in a
separate deployment repository.

```
docker/Dockerfile      one image, API and both worker profiles
infrastructure/
  postgres/initdb/     extensions for the local Compose stack
  rustfs/              staging bucket, scoped identities, and their provisioning
  reset/               wipe a local or Kubernetes environment back to an empty ledger and bucket prefix (docs/reset.md)
  staging-compatibility.md   RustFS/OVH staging evidence matrix
```

| Concern | Where |
| --- | --- |
| Container image | `docker/Dockerfile` here |
| Build and publish | `.github/workflows/docker-publish.yml` here |
| Kubernetes manifests | the deployment repository |
| Operators, storage class, image policies | the deployment repository |

## One image, three roles

The API and both worker profiles run the same image and differ only in `command:` and
environment: `CURATE_D_WORKER_PROFILE`, the executor identity and the staging role and
credentials. This is not only about build cost — layers are shared, so two
images would occupy the same space locally. It is because **DBOS pins in-flight workflows
to an application version**: two independently-resolved image tags could leave the API
enqueueing work on a version no worker is running, which surfaces as workflows sitting
`ENQUEUED` while every health check reports green. One image makes that unreachable.

```bash
docker build -f docker/Dockerfile -t curate-d:dev .

docker run --rm -p 8000:8000 \
  -e CURATE_D_LEDGER_DATABASE_URL=postgresql+psycopg://curate_d:curate_d@host.docker.internal:5433/curate_d \
  curate-d:dev                                    # API (the image default)

docker run --rm \
  -e CURATE_D_LEDGER_DATABASE_URL=... \
  -e CURATE_D_WORKER_PROFILE=io \
  -e CURATE_D_EXECUTOR_ID=worker-io-0 \
  curate-d:dev python -m curate_d.worker          # worker; also needs its staging settings (compose.yaml)
```

## Releasing

| Push | Tag produced |
| --- | --- |
| branch `dev` | `dev-<UTC timestamp>-<sha>` |
| branch `main`, untagged | nothing |
| tag `v0.2.0` on `main` | `v0.2.0`, `v0.2` |

The two tag streams are meant for different environments: semver tags for production,
timestamp ordering over `dev-*` tags for development. How a deployment selects a tag is up to
its own repository.

The rule for production is **a semver tag whose commit is on `main`, and nothing else**.
The flow is: bump `version` in `pyproject.toml`, merge `dev` to `main`, then tag the
merge commit on `main` `vX.Y.Z`.

Nothing is published that has not passed the merge gate. `dev` and `main` change only by
pull request, and a pull request merges only when its head is up to date with its base and
lint, the in-workflow database-access audit, strict type checking and the unit suite pass
(`checks.yml`). When it changes a path the integration suite depends on, the full
integration suite against the `make test-stack` services and a boot of the image built from
that commit must pass too (`integration.yml`); otherwise that check reports green with both
skipped. The build does not test again: an image that fails its own tests only moves the
discovery into the cluster, so the tests run before the merge.

Three guards enforce it, because any one alone leaves a gap:

1. The tag trigger matches the semver shape, not `v*`, so `v1` or `vfoo` never starts a
   build.
2. The tagged commit must be an ancestor of `main`. Git will let a tag be created on an
   unmerged commit; the pipeline refuses to release one.
3. The tag must equal the packaged version. DBOS pins in-flight workflows to that
   version, so a `v0.2.0` image built from a `0.1.0` package would keep enqueueing and
   recovering work as `0.1.0` with nothing reporting the mismatch.

CI detects violations; it cannot prevent the tag existing. A GitHub ruleset restricting
tag creation is the complement if you want that too.

A dev image is built with its own tag as `CURATE_D_BUILD_REVISION`, so it runs as the
DBOS application version `<packaged version>+dev.<timestamp>.<sha>`; a release image has
none and runs as the bare packaged version the tag guard checked. Every build is
therefore a distinct version, and DBOS's latest version is the newest build. That is
what decides which workers may apply and delete persisted schedules: an older build
restarting mid-rollout is never the latest, so it cannot remove a schedule a newer one
added. The cost is DBOS's version pinning applied per dev build: work a previous dev
build left queued or in flight is not taken up by the new one until the recovery sweep
moves it, as after a release.

Rolling application code back cannot lose files: the ledger is additive and placements
never modify source content, so the worst outcome is placements the older version does
not act on, which reconciliation resolves once rolled forward. Schema changes must stay
backward-compatible within a release series so a pod on the old version can still read
the new schema mid-rollout.

## Content staging on RustFS

Local development and CI stage media in RustFS through the same S3 adapter the cluster
uses against OVH Standard S3. Only the endpoint, signing region, addressing style, TLS
trust and credentials differ, and all five are configuration. The S3 API is on
`127.0.0.1:9100` and the console on `127.0.0.1:9101`, both loopback-only. RustFS replaced
MinIO, whose upstream is archived; the reasons and engine notes are in
[`docs/staging.md`](../docs/staging.md#local-engine-rustfs).

```bash
make stack-up          # brings up RustFS and runs provisioning
make staging-init      # re-run provisioning alone, printing each step
make lifecycle-check   # observe expiration and abort reclamation (rows 30/31) in seconds
```

`infrastructure/rustfs/initialize.sh`, run by `rustfs-init` with RustFS's own CLI `rc`,
creates the bucket (`CURATE_D_STAGING_S3_BUCKET`, default `curate-d-staging`), sets SSE-S3
as its default, imports the expiration and `AbortIncompleteMultipartUpload` lifecycle rules,
provisions the two identities from the policy templates in
`infrastructure/rustfs/policies/`, which `infrastructure/rustfs/render_policy.sh` renders for
the bucket and prefix (`CURATE_D_STAGING_S3_CONTENT_PREFIX`, default `content/`) because `rc`
imports a policy as written, and then **proves the access matrix with those
identities**, including an encrypted probe write, before exiting zero. Both workers wait
on that exit code, so a partially provisioned backend stops the stack rather than
surfacing later as a staging failure.

| Identity | Held by | May |
| --- | --- | --- |
| `curate-d-staging-writer` | `worker-io` | read, write, delete and manage multipart uploads under `content/`; list the bucket's objects and multipart uploads |
| `curate-d-staging-reader` | `worker-cpu` | `GetObject` under `content/`, nothing else |

Neither may create buckets or edit policies, and neither is the root identity. The
reader cannot write or leave the prefix.

One deviation from the OVH policies: both RustFS policies also grant `<bucket>/content`,
without the slash. RustFS evaluates a policy against the object key with its trailing
slash removed, so the readiness marker at `content/` is checked as `content` and no
`content/*` or `content/` grant covers it. That widens each identity by exactly the one
key `content`, which the application never writes;
`test_neighbouring_keys_stay_denied` pins the neighbouring keys (`contentx`,
`content-other/…`, `content.<id>`) as denied.

Initialization fails rather than degrades. RustFS starts without a valid
`RUSTFS_SSE_S3_MASTER_KEY` but refuses every SSE-S3 write, so initialization's encrypted
probe turns a missing or malformed key into a failed stack-up instead of storing staged
media in the clear.

**RustFS results are not evidence about OVH.** Both engines accept the same
`AbortIncompleteMultipartUpload` rule, and RustFS enforces it: `make lifecycle-check`
runs a separate, disposable RustFS whose lifecycle day lasts one second
(`compose.lifecycle.yaml`) and observes the rule reclaim an abandoned upload and the
expiration rule remove an aged object. The development stack keeps real day lengths.
Both images are pinned by digest, because the compatibility evidence is only meaningful
against an exact build.

```bash
uv run pytest tests/integration/test_staging_contract.py -m integration --no-cov \
  --staging-backend=rustfs
uv run pytest tests/integration/test_staging_backend_checks.py -m integration --no-cov \
  --staging-backend=rustfs
```

## rclone remote config is encrypted

The rclone sidecar keeps one remote per connected provider account, with the account's
access token in its config file. The token is also stored encrypted in the ledger, so the
config file is a second copy and is encrypted too, with `RCLONE_CONFIG_PASS`.

Setting that variable alone does not do it: rclone encrypts a config only once the file
was created through `rclone config encryption set`, which reads the password from stdin.
The sidecar therefore starts through a small wrapper that runs that command when the file
is missing or still plain text, refuses to start without a password, and then executes
the daemon (see the `rclone` service in `compose.yaml`; a Kubernetes sidecar needs the
same wrapper and the password from a Secret that only the sidecar mounts, so the worker
container never sees it). Remotes are reconfigured from the ledger on first use, so a
new password only needs a restart of the sidecar and an empty or plain config volume.
`tests/integration/test_rclone.py` asserts the file on disk holds no plain token.

The control API itself is unauthenticated and returns remotes decrypted (`config/dump`):
keep it reachable only from the I/O worker.

## Deployment manifests

The manifests live in a separate deployment repository, and so do the checks on them. This
repository never reads that one: no source file, test or Make target looks for a
checkout beside it, so what passes here is exactly what CI verified.

The manifests still depend on names this codebase defines, and those are the contract
the deployment repository's checks have to hold to:

| Contract | Defined here |
| --- | --- |
| Worker profile names in `CURATE_D_WORKER_PROFILE` | `curate_d.runtime.profiles` |
| Executor identity in `CURATE_D_EXECUTOR_ID`, distinct and stable per pod | `curate_d.runtime.identity` |
| Environment names in `CURATE_D_ENVIRONMENT` (`dev`, `prod`), naming the DBOS application `curate-d-<env>` on the API and both workers and labelling each process's telemetry | `curate_d.config.DeploymentEnvironment`, `curate_d.runtime.dbos_app.application_name` |
| Queues each profile serves | `curate_d.runtime.queues` (tested here; manifests carry only the profile) |
| Dev image tag `dev-<UTC timestamp>-<sha>` | `.github/workflows/docker-publish.yml` (pinned by `test_release_pipeline.py`) |

## Restore drill

The ledger is the only record of which files exist where and cannot be reconstructed from
the providers, so point-in-time recovery is a requirement rather than a nicety. Run the
drill after any change to backup configuration.

The procedure lives with the manifests, in the deployment repository.

## Provisioning smoke check

Run `uv run python -m curate_d.staging.smoke --cross-bucket <unrelated-bucket>` after
provisioning each backend. It uploads with the writer, reads and verifies size/SHA-256
with the reader, proves reader PUT/DELETE denial, proves both identities' cross-bucket
GET denial, and deletes its probes. Every run uses unique attempt keys under the configured
content prefix. Failed reads and upload verification still trigger cleanup; cleanup failure
is a failed result too.

Exit **0** means every check passed, **1** means a check failed, and **2** means invalid
arguments/configuration or an invocation that could not run. Missing objects, missing
buckets and unrelated request failures never count as denied access. Output contains
fixed check names and diagnostics, with no credentials, configuration values, backend
exception text or tracebacks. Inspect backend logs privately when a fixed diagnostic
needs more context.

This command is a **provisioning-time access-matrix check**, requiring both identities.
Worker readiness uses only the worker's own role credential and never imports this
command. Do not inject the smoke-reader variables into either worker. The command has
no administrator credentials and cannot create buckets or change policies.

After every access check and cleanup succeeds, the command seeds a zero-byte readiness
marker at the exact configured prefix (`content/` by default) using only the writer
identity, then verifies it with a GET. Marker failure makes the command fail. The marker
is retained and excluded from application object occupancy. Writer startup also seeds it,
and each idle cleanup tick refreshes it before lifecycle expiration can age it out. CPU
readiness must GET that existing marker and verify it is empty using only the reader
identity; a missing marker or denied GET fails startup. Run provisioning smoke or start
the writer before CPU workers. If all writers remain offline long enough for lifecycle
expiration to remove the marker, restart a writer or rerun smoke before restarting CPU
workers. The marker does not exempt media objects from the normal expiration backstop.

### RustFS invocation

Start the local stack with `make stack-up`. Separately provision an unrelated disposable
bucket for the denial probe using the local initialization identity:

```bash
docker compose run --rm --no-deps --entrypoint /bin/sh rustfs-init -eu -c '
  rc alias set admin "$RUSTFS_ENDPOINT" "$RUSTFS_ROOT_USER" "$RUSTFS_ROOT_PASSWORD" >/dev/null
  rc bucket create --ignore-existing admin/curate-d-smoke-ungranted >/dev/null
'
```

Run with the normal local identities (override these development defaults if local
provisioning uses other credentials). The local bucket and prefix default to
`curate-d-staging` and `content/`. Export `CURATE_D_STAGING_S3_BUCKET` or
`CURATE_D_STAGING_S3_CONTENT_PREFIX` (the prefix ends with `/`) before `make stack-up` or
`make staging-init` to change them: the workers and `rustfs-init` read the same variables, and
`rustfs-init` renders the policies for them. Use the same values in the command below.

```bash
CURATE_D_STAGING_S3_ENDPOINT_URL=http://127.0.0.1:9100 \
CURATE_D_STAGING_S3_BUCKET=curate-d-staging \
CURATE_D_STAGING_S3_REGION=us-east-1 \
CURATE_D_STAGING_S3_ADDRESSING_STYLE=path \
CURATE_D_STAGING_S3_VERIFY_TLS=false \
CURATE_D_STAGING_S3_CONTENT_PREFIX=content/ \
CURATE_D_STAGING_S3_ROLE=writer \
CURATE_D_STAGING_S3_ACCESS_KEY_ID=curate-d-staging-writer \
CURATE_D_STAGING_S3_SECRET_ACCESS_KEY=writer-development-secret \
CURATE_D_STAGING_SMOKE_READER_ACCESS_KEY_ID=curate-d-staging-reader \
CURATE_D_STAGING_SMOKE_READER_SECRET_ACCESS_KEY=reader-development-secret \
uv run python -m curate_d.staging.smoke --cross-bucket curate-d-smoke-ungranted
```

The unrelated bucket must already exist, be private and grant neither staging identity
access. The smoke command never writes there. Remove this disposable bucket separately
with the same local provisioning identity after checking (`rc bucket remove
admin/curate-d-smoke-ungranted`, run through `rustfs-init` as above); no worker should gain
that permission.

### OVH invocation

Provision separate staging writer/reader **S3 credentials** and a private unrelated
disposable bucket first. OVH management credentials (`OVH_APPLICATION_KEY`,
`OVH_APPLICATION_SECRET`, `OVH_CONSUMER_KEY`) are not S3 access keys. Export the four
application credential variables shown below through the provisioning secret delivery
mechanism; do not put real values in shell history or enable shell tracing.

```bash
# Required environment, supplied by the provisioning secret mechanism:
# CURATE_D_STAGING_S3_ACCESS_KEY_ID
# CURATE_D_STAGING_S3_SECRET_ACCESS_KEY
# CURATE_D_STAGING_SMOKE_READER_ACCESS_KEY_ID
# CURATE_D_STAGING_SMOKE_READER_SECRET_ACCESS_KEY
# STAGING_BUCKET and STAGING_DENIED_BUCKET: pre-provisioned disposable bucket names.
CURATE_D_STAGING_S3_ENDPOINT_URL=https://s3.us-east-va.io.cloud.ovh.us \
CURATE_D_STAGING_S3_BUCKET="$STAGING_BUCKET" \
CURATE_D_STAGING_S3_REGION=us-east-va \
CURATE_D_STAGING_S3_ADDRESSING_STYLE=virtual \
CURATE_D_STAGING_S3_VERIFY_TLS=true \
CURATE_D_STAGING_S3_CONTENT_PREFIX=content/ \
CURATE_D_STAGING_S3_ROLE=writer \
uv run python -m curate_d.staging.smoke --cross-bucket "$STAGING_DENIED_BUCKET"
```

Keep certificate verification enabled for OVH and use virtual-hosted addressing. Select
`--log-format console` for terminal output; JSON is the default. Record the command's
exit status, endpoint, SDK versions, date and results in the
[compatibility evidence matrix](staging-compatibility.md). Missing OVH S3 credentials
leave verification **PENDING** and activation blocked; a RustFS pass does not change that.
Encryption, actual storage tier, lifecycle execution and bucket-creation latency require
separate provisioning evidence and backend-specific tests.
