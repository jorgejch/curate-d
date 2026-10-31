# Design

## Context

See proposal.md - Why. Today `main()` in `src/curate_d/api/__main__.py` passes
`host="0.0.0.0"` to `uvicorn.run` and resolves the port with `listen_port()`, which reads
`CURATE_D_API_LISTEN_PORT` from `os.environ`, falls back to a default, and raises `ValueError`
naming the variable on a bad value. The API runs in three ways:

```
 how it runs                    needs
 -----------------------------  ------------------------------------------------
 local, compose (container)     all interfaces in the container; host publishes
 dev / prod, k8s (container)    all interfaces in the pod (Service, probes)
 local, bare python -m          loopback only
```

`docker/Dockerfile` builds the one image every workload runs (API and both worker profiles);
compose and the Kubernetes manifests pick the process with `command:`. The manifests live in
the infrastructure repository, which this repository must not read (CLAUDE.md, Repository
boundaries).

## Goals / Non-Goals

**Goals:**
- A safe default for a bare run, and the right bind in every container, from one setting.
- No change in the infrastructure repository.
- Close the host-side exposure of the local stack in the same change.

**Non-Goals:**
- Authentication on the API.
- Choosing the listen address from `CURATE_D_ENVIRONMENT`.
- Dual-stack defaults: the image binds IPv4 `0.0.0.0`, as it does today. An IPv6-only cluster
  sets `::` through the variable.
- Moving `listen_port()` or the new setting into `Settings`.

## Decisions

### D1. Loopback default in code, all interfaces set by the image

`listen_host()` returns `127.0.0.1` when `CURATE_D_API_LISTEN_HOST` is unset; the Dockerfile's
existing `ENV` block gains `CURATE_D_API_LISTEN_HOST=0.0.0.0`.

The image is the one artifact that knows the process runs in its own network namespace, where
all interfaces is the correct bind and the namespace is the boundary. Setting the value there
covers compose, dev and prod at once and removes the literal from the Python code, so the
linter finding goes away without a suppression.

Alternatives:
- *Default `0.0.0.0`, override to loopback for a bare run*: keeps the unsafe value as the
  default and the finding in place.
- *Keep the literal with `# noqa: S104`*: a bare run still serves the LAN.
- *Choose by `CURATE_D_ENVIRONMENT`*: compose runs `local` in a container, so "local means
  loopback" breaks the stack.
- *Set the variable in compose and the manifests*: three places to keep in step, one of them
  in another repository.

### D2. Read the variable like `listen_port()`

`LISTEN_HOST_ENV = "CURATE_D_API_LISTEN_HOST"` and `listen_host()` sit beside the port code and
follow its shape: `os.environ`, a default, `ValueError` naming the variable and value. Two
listener settings resolved two ways would be harder to read than either.

The name avoids Kubernetes service-link collisions for the same reason `LISTEN_PORT` does:
links inject `<SERVICE>_SERVICE_HOST` and `<SERVICE>_PORT*`, never `*_LISTEN_HOST`.

### D3. Accept IP literals only

Validate with `ipaddress.ip_address` and return the normalised text. A host name would make
the bound interface depend on resolver state at startup, and `localhost` can resolve to `::1`
alone; the project prefers an explicit failure over an inferred value. The check runs before
`uvicorn.run`, so a bad value never reaches a socket.

### D4. Loopback publishing in compose, pinned by a test

`api` becomes `"127.0.0.1:${CURATE_D_API_PORT:-8000}:8000"` and `postgres`
`"127.0.0.1:${CURATE_D_POSTGRES_PORT:-5433}:5432"`, each with a one-line reason as RustFS and
rclone carry. A new unit test in `tests/unit/infrastructure/` parses every `ports:` entry in
`compose.yaml` and `compose.lifecycle.yaml` and fails naming any that does not start with
`127.0.0.1:`; it also asserts the Dockerfile sets `CURATE_D_API_LISTEN_HOST=0.0.0.0`. Precedent:
`test_dockerfile_pins.py`, `test_rustfs_provisioning.py`.

This is local-stack configuration, not system behaviour, so it has no requirement; the test is
its contract.

## Risks / Trade-offs

- [A dev or prod manifest sets its own `env:` that clears or overrides the variable] → The API
  would bind loopback and fail its probes, so the rollout stops on the old ReplicaSet rather
  than serving broken. Check the manifests in the infrastructure repository before merge.
- [`localhost:5433` resolves to `::1` first on macOS while compose publishes IPv4 only] →
  libpq tries each resolved address in turn, so the connection falls through to `127.0.0.1`.
  The integration run confirms it.
- [A developer reached the local stack from another device] → That access ends; it was the
  exposure being removed. Override the mapping in a local `compose.override.yaml` if needed.
- [Workers inherit an `ENV` they do not read] → No effect; only the API entry point reads it.

## Migration Plan

Ship in one release. The image carries the setting, so dev picks it up with the next dev image
and prod with the next release; no manifest change and no ordering. Rollback is the previous
image, which binds `0.0.0.0` unconditionally.
