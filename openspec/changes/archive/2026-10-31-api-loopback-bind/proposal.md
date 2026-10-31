# Proposal

## Why

The API entry point hardcodes `host="0.0.0.0"` (`src/curate_d/api/__main__.py`), so a bare
`python -m curate_d.api` on a laptop serves the unauthenticated control plane to the local
network, and linters flag the literal as a bind to all interfaces. The compose stack has the
same exposure one layer out: it publishes the API (`8000`) and PostgreSQL (`5433`, development
credentials) on every host interface, while RustFS and rclone in the same file already publish
on loopback only for that reason.

The fix must hold in local, dev and prod. The deciding axis is container versus bare process,
not `CURATE_D_ENVIRONMENT`: compose runs with `CURATE_D_ENVIRONMENT=local` inside a container,
where a loopback bind would break port publishing, and Kubernetes (dev, prod) needs an
all-interfaces bind for the Service, probes and ingress.

## What Changes

- The API binds `127.0.0.1` unless `CURATE_D_API_LISTEN_HOST` names another address. The value
  must be an IP literal (IPv4 or IPv6); anything else stops the API at startup with the variable
  and value named.
- `docker/Dockerfile` sets `ENV CURATE_D_API_LISTEN_HOST=0.0.0.0`, so every container started
  from the image (compose locally, Kubernetes in dev and prod) binds all interfaces inside its
  own network namespace with no manifest change.
- `compose.yaml` publishes the `api` and `postgres` ports on `127.0.0.1` only, matching
  `rustfs` and `rclone`.
- A unit test pins that every port `compose.yaml` publishes is loopback-only and that the image
  sets the listen host.

No **BREAKING** change for the deployed environments, on the assumption stated in the design:
the dev and prod manifests do not clear `CURATE_D_API_LISTEN_HOST`. A developer who reaches the
local stack from another machine loses that access; it was the exposure being removed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `control-plane`: adds the requirement "API listen address": loopback by default, an
  IP-literal override through `CURATE_D_API_LISTEN_HOST`, startup failure on an invalid value,
  and the container image setting all interfaces.

## Impact

- Code: `src/curate_d/api/__main__.py` (new `listen_host()` beside `listen_port()`).
- Image: `docker/Dockerfile` (one `ENV` entry). Workers ignore the variable.
- Local stack: `compose.yaml` (`api` and `postgres` port mappings).
- Tests: `tests/unit/api/test_main.py`; a new `tests/unit/infrastructure/` test.
- CI: none expected. `integration.yml` runs the suite on the runner host against the published
  ports, and its readiness probe already targets `127.0.0.1:8000`.
- Infrastructure repository: nothing to change; confirm there that no manifest overrides the
  variable.
