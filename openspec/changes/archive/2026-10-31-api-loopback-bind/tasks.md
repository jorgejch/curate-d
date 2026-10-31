# Tasks

Groups 1 and 2 touch disjoint files and can start at once. Group 3 waits on both.

## 1. API listen host

- [x] 1.1 In `src/curate_d/api/__main__.py`, add `DEFAULT_HOST = "127.0.0.1"`, `LISTEN_HOST_ENV = "CURATE_D_API_LISTEN_HOST"` and `listen_host()` beside `listen_port()` (design D2, D3): unset returns the default; a value `ipaddress.ip_address` accepts returns its normalised text; anything else raises `ValueError` naming the variable and the value. Add tests to `tests/unit/api/test_main.py`, clearing the variable in the autouse fixture: unset gives `127.0.0.1`; `0.0.0.0` and `::` are returned; `localhost`, `""` and `0.0.0.0:8000` each fail naming the variable and value; with the variable unset, `CURATE_D_ENVIRONMENT` `local`, `dev` and `prod` all give `127.0.0.1`. Verify `uv run pytest tests/unit/api/test_main.py` passes
- [x] 1.2 In `main()`, replace `host="0.0.0.0"` with `host=listen_host()`, resolved before `uvicorn.run` is called. Add tests: `main()` hands uvicorn the resolved host; with an invalid value `main()` raises and `uvicorn.run` is never called. Verify `uv run pytest tests/unit/api/test_main.py` passes and `grep -n '0\.0\.0\.0' src/curate_d/api/__main__.py` finds nothing

## 2. Image and local stack

- [x] 2.1 In `docker/Dockerfile`, add `CURATE_D_API_LISTEN_HOST=0.0.0.0` to the runtime `ENV` block with a one-line comment on why the image, not the code, sets it (design D1). Verify `docker build -f docker/Dockerfile -t curate-d:dev .` succeeds and `docker run --rm curate-d:dev env | grep CURATE_D_API_LISTEN_HOST` prints `CURATE_D_API_LISTEN_HOST=0.0.0.0`
- [x] 2.2 In `compose.yaml`, publish `api` as `"127.0.0.1:${CURATE_D_API_PORT:-8000}:8000"` and `postgres` as `"127.0.0.1:${CURATE_D_POSTGRES_PORT:-5433}:5432"`, each with a one-line reason like the `rustfs` and `rclone` entries carry (design D4). Verify `docker compose config | grep -A4 'published'` shows `host_ip: 127.0.0.1` on every published port
- [x] 2.3 Add `tests/unit/infrastructure/test_loopback_exposure.py`: every `ports:` entry in `compose.yaml` and `compose.lifecycle.yaml` starts with `127.0.0.1:`, and a failure names the service and entry; `docker/Dockerfile` sets `CURATE_D_API_LISTEN_HOST=0.0.0.0`. Verify `uv run pytest tests/unit/infrastructure/test_loopback_exposure.py` passes, and fails naming `api` when its mapping is reverted locally

## 3. Integration

- [x] 3.1 Bring the stack up with `make stack-up` and verify `curl -fsS http://127.0.0.1:8000/health/ready` and `curl -fsS http://localhost:8000/health/ready` both succeed
- [x] 3.2 Run `make test-stack`, then `make test-integration`, and verify the suite passes against the default `localhost:5433` DSN (design, Risks: `::1` falls through to `127.0.0.1`)
- [x] 3.3 With the compose `api` service stopped, run `uv run python -m curate_d.api` outside a container and verify `lsof -nP -iTCP:8000 -sTCP:LISTEN` shows `127.0.0.1:8000` only
- [x] 3.4 Verify `make lint`, `make typecheck`, `make test` and `openspec validate api-loopback-bind --strict` pass

## Workflow follow-up

- Before merge, confirm in the infrastructure repository that no dev or prod manifest sets or clears `CURATE_D_API_LISTEN_HOST` (design, Risks). This repository cannot check it.
- After the next dev image rolls out, check that the API pods pass readiness.
- Sync the delta into `openspec/specs/control-plane/spec.md` and archive the change.
