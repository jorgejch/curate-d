# Design

## Context

`rc admin policy create` imports a policy file as written, so the bucket and prefix cannot be
variables inside it. The fixed-bucket change resolved that by making the policies the source.
Making the values configurable inverts it: the variables are the source and the policies are
rendered from them at provisioning time.

## Decisions

### Render the policies in `rustfs-init`, from templates

The two policy files keep their paths and carry placeholders inside JSON strings, so they
remain valid JSON: `@BUCKET@`, `@PREFIX@` (with its trailing slash) and `@PREFIX_KEY@` (without
it, the extra grant `initialize.sh` documents for the readiness marker). `render_policy.sh`
substitutes them with `sed` and writes to stdout; `initialize.sh` writes the result to `/tmp`
and passes that to `rc`. The script is separate from `initialize.sh` so the unit suite can run
it without `rc`.

Alternatives considered:
- **Generate the policy with `jq` in `initialize.sh`.** Needs `jq` in the pinned image and
  hides the grant shape in a filter; the templates read as the policy they produce.
- **Keep the files fixed and refuse other values.** The decision just reversed.

### Validate before substituting

An unchecked value could break the JSON or widen a grant. `render_policy.sh` accepts a bucket
by S3 naming (3 to 63 of `a-z0-9.-`, alphanumeric ends) and a prefix in `[A-Za-z0-9._/-]`
that is relative, ends in `/` and has no empty segment, matching `Settings`'
`staging_s3_content_prefix` rules. The charset also keeps `sed`'s `|` delimiter and `&` out
of the replacement. It fails before `initialize.sh` creates a bucket.

### One variable feeds workers and provisioning

Both read `CURATE_D_STAGING_S3_BUCKET` and `CURATE_D_STAGING_S3_CONTENT_PREFIX` with the same
defaults, restoring the pre-#114 wiring. The unit test fails if either side drifts.

## Risks / Trade-offs

- [Templated files are not importable by hand] → documented in the README; only `rustfs-init`
  reads them, and the unit test renders each.
- [`sed` and `case` run in the pinned image's BusyBox] → both are POSIX; the script is run
  with `sh` as `initialize.sh` is. Verify with `make test-stack`.
- [An existing volume provisioned for another bucket] → `rustfs-init` converges on the new
  names and leaves the old bucket in place; remove it by hand.

## Migration Plan

None for defaults. A developer who exports the variables gets that bucket on the next
`make stack-up`.
