# Proposal

## Why

Dependabot security updates are on for `jorgejch/curate-d` and its pull requests already
get a SonarQube analysis (`release-pipeline`, "The SonarQube scan skips only fork pull
requests"), but nothing proposes routine version updates: `pyproject.toml` ranges and
`uv.lock` drift until someone bumps them by hand, and the two base images in
`docker/Dockerfile` are set through `ARG`-interpolated `FROM` lines Dependabot cannot
read.

## What Changes

- Add `.github/dependabot.yml` with three version-update ecosystems, every one targeting
  `dev` on a weekly Friday-afternoon (America/Los_Angeles) schedule:
  - `uv` at `/`, `versioning-strategy: increase`: patches in one weekly group, one pull
    request per minor and per major. `aioboto3` ignores `semver-minor` (patches still flow)
    because a new minor needs re-verifying against RustFS and OVH first
    (`pyproject.toml:64-68`; the OVH side is #48).
  - `github-actions` at `/`: patches and minors in one weekly group, one pull request per
    major.
  - `docker` at `/docker`: patches in one weekly group (a digest-only refresh opens on its own), one pull
    request per minor and per major. The `python` image ignores `semver-minor` and
    `semver-major`: the repository targets 3.12 (`.python-version`, `requires-python`, the
    classifiers), and a Dockerfile-only bump would move the image off the version
    everything else declares.
- Rewrite `docker/Dockerfile`'s `FROM` lines as literal `name:tag@sha256:<digest>`
  (`uv` image and both `python` stages), dropping `ARG PYTHON_VERSION` and
  `ARG UV_VERSION`. `ARG TESSERACT_VERSION` stays: it is an apt pin Dependabot cannot
  reach. Nothing passes the two dropped args at build time (the only build arg anywhere is
  `CURATE_D_BUILD_REVISION`). Digests are resolved from the registry at apply time.
- Pin the config and the Dockerfile shape with unit tests under
  `tests/unit/infrastructure/`, in the style of `test_checks_workflow.py`.
- Security updates stay as GitHub raises them; the config's groups apply to version
  updates only.

Out of scope: Compose image updates (the production versions live in the Flux manifests in
another repository, `postgis` sits behind `${CURATE_D_POSTGRES_IMAGE:-...}`, and the
RustFS digests are evidence recorded in `infrastructure/staging-compatibility.md`); the
OVH re-verification suite (#48); any workflow change (`checks.yml` already handles a
Dependabot run's `SONAR_TOKEN`).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: adds requirements for how dependency version updates are proposed:
  target branch, cadence, grouping per ecosystem, the `aioboto3` minor exception, the
  `python` image minor/major exception, and the Dockerfile `FROM` shape Dependabot needs,
  each pinned by a unit test.

## Impact

- New: `.github/dependabot.yml`, `tests/unit/infrastructure/test_dependabot_config.py`,
  `tests/unit/infrastructure/test_dockerfile_pins.py`.
- Modified: `docker/Dockerfile` lines 16-17, 20, 26, 53 (`ARG`/`FROM`). `AS uv`,
  `AS builder` and `AS runtime` stay (`test_release_pipeline.py:197-202` splits on
  `AS runtime`; `COPY --from=uv` stays valid).
- `docker/Dockerfile` is a declared path, so this pull request runs the whole integration
  suite and the image boot; `.github/dependabot.yml` is not, which is right (the suite
  does not read it).
- Ongoing: Dependabot pull requests into `dev` that touch `pyproject.toml`, `uv.lock` or
  `docker/Dockerfile` run the full integration suite, and each merge makes the others stale
  under the up-to-date-with-base rule. That is why patches are grouped and minors are not.
- Dockerfile-only bumps from the `docker` ecosystem (a new `python:3.12.x` patch or a
  digest refresh) change the image the integration job boots; a failure there surfaces on
  the Dependabot pull request and blocks nothing else.
