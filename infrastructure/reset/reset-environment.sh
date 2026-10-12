#!/usr/bin/env bash
# Reset a curate.d environment to empty: the ledger, including the DBOS system schema, and
# the staging bucket's content prefix. Provider connections are kept unless asked not to,
# so pCloud stays authorized. Only `local` and `dev`; there is no production path.
#
#   local  The Compose stack. Stops the API and both workers, resets, migrates, then
#          restarts every service except Postgres and waits for them to be healthy.
#   dev    The Kubernetes namespace in CURATE_D_RESET_NAMESPACE, through your kubectl
#          context. Optionally keeps a GitOps controller off the API and both workers (the
#          annotation in CURATE_D_RESET_PAUSE_ANNOTATION, set to `disabled`), scales them
#          to zero, resets in a one-off pod built from the I/O worker's pod template, then
#          restarts the pooler and the optional egress proxy, scales the API and the
#          workers back, and removes the annotation. The database pod keeps running.
#
# The wipe takes DBOS's persisted schedules with it, and workers never write them, so both
# end by running the deploy job's schedule step (`python -m curate_d.schedules reconcile`)
# once the workers are back: locally the `schedules` service, in dev a one-off pod.
#
# Dev settings (environment variables):
#   CURATE_D_RESET_NAMESPACE         Required. Namespace of the environment to reset.
#   CURATE_D_RESET_CONFIGMAP         ConfigMap holding CURATE_D_ENVIRONMENT (curate-d-config).
#   CURATE_D_RESET_API               API workload (deployment/curate-d-api).
#   CURATE_D_RESET_WORKER_IO         I/O worker workload (statefulset/curate-d-worker-io).
#   CURATE_D_RESET_WORKER_CPU        CPU worker workload (statefulset/curate-d-worker-cpu).
#   CURATE_D_RESET_POOLER            Pooler to restart (deployment/curate-d-db-pooler-rw);
#                                    set to an empty string to skip.
#   CURATE_D_RESET_EGRESS_PROXY      Egress proxy to restart (empty, skipped by default).
#   CURATE_D_RESET_PAUSE_ANNOTATION  Annotation that keeps a GitOps controller off the
#                                    workloads while they are at zero (empty, skipped).
#
# The database and bucket work is `reset.py`, run with `python -c` inside the curate.d
# image (locally `docker compose run worker-io`, in dev the one-off pod), so it uses that
# worker's settings and staging writer identity and needs no credentials on this machine.
# It refuses an environment other than the one its container is configured for.
# docs/reset.md explains the steps, what is kept, and recovery.
#
# Without --execute it only prints what a reset would remove. --execute asks you to type
# `reset <env>` first, unless --yes is given.
#
# If a dev reset fails after the scale-down, the API and workers stay at zero (and paused,
# if an annotation is configured); rerun the script, or hand them back by removing the
# `curate-d/reset-replicas` annotation (and the pause annotation) from each workload and
# scaling them to the recorded count.
#
# Usage:
#   infrastructure/reset/reset-environment.sh local|dev [--execute [--yes]] [--drop-connections]
set -euo pipefail

usage() {
  sed -n '/^# Usage:/,/^set /p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
ENVIRONMENT=$1
shift
case $ENVIRONMENT in local | dev) ;; *) usage ;; esac

EXECUTE=false YES=false WIPE_FLAGS=""
for argument in "$@"; do
  case $argument in
    --execute) EXECUTE=true ;;
    --yes) YES=true ;;
    --drop-connections) WIPE_FLAGS=--drop-connections ;;
    *) usage ;;
  esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
RESET_PY=$(<"$HERE/reset.py")
# Run in the container: everything after the plan, so the schema is never left half-built
# by a wipe that succeeded and a migration that was never started.
RESET_STEPS="python -c \"\$RESET_PY\" wipe $ENVIRONMENT $WIPE_FLAGS \
  && alembic upgrade head && python -c \"\$RESET_PY\" restore $ENVIRONMENT"
PLAN_STEP="python -c \"\$RESET_PY\" plan $ENVIRONMENT"

NAMESPACE=${CURATE_D_RESET_NAMESPACE:-}
CONFIGMAP=${CURATE_D_RESET_CONFIGMAP:-curate-d-config}
WORKER_IO=${CURATE_D_RESET_WORKER_IO:-statefulset/curate-d-worker-io}
APP_WORKLOADS=("${CURATE_D_RESET_API:-deployment/curate-d-api}" "$WORKER_IO"
  "${CURATE_D_RESET_WORKER_CPU:-statefulset/curate-d-worker-cpu}")
POOLER=${CURATE_D_RESET_POOLER-deployment/curate-d-db-pooler-rw}
EGRESS_PROXY=${CURATE_D_RESET_EGRESS_PROXY:-}
PAUSE_ANNOTATION=${CURATE_D_RESET_PAUSE_ANNOTATION:-}
# No dots: kubectl jsonpath reads the key unescaped.
RECORD=curate-d/reset-replicas
HELPER_POD=curate-d-reset
K=(kubectl -n "${NAMESPACE:-unset}")

log() { printf '\n== %s\n' "$*"; }
fail() {
  echo "reset-environment: $*" >&2
  exit 1
}

confirm() {
  $YES && return
  [[ -t 0 ]] || fail "--execute needs a terminal to confirm, or --yes"
  local answer
  read -r -p "Type 'reset $ENVIRONMENT' to wipe it: " answer
  [[ $answer == "reset $ENVIRONMENT" ]] || fail "not confirmed; nothing changed"
}

# --- local ---------------------------------------------------------------------------

local_helper() {
  docker compose run --rm --no-deps -T -e RESET_PY="$RESET_PY" worker-io sh -c "$1"
}

local_reset() {
  [[ -n ${CURATE_D_CREDENTIAL_ENCRYPTION_KEY:-} ]] ||
    fail "export the stack's CURATE_D_CREDENTIAL_ENCRYPTION_KEY first, as for make stack-up"
  local service
  for service in postgres rustfs; do
    [[ -n $(docker compose ps --status running -q "$service") ]] ||
      fail "$service is not running; start the stack with make stack-up"
  done

  log "What a reset removes"
  local_helper "$PLAN_STEP"
  $EXECUTE || { echo -e "\nDry run; add --execute to reset."; return; }
  confirm

  log "Stopping the API and both workers"
  docker compose stop api worker-io worker-cpu
  log "Resetting"
  local_helper "$RESET_STEPS" || fail "reset failed; the API and workers stay stopped"

  log "Restarting every service except Postgres"
  # shellcheck disable=SC2046 # one word per service name
  docker compose stop $(docker compose config --services | grep -vx postgres)
  docker compose up -d --wait
  log "Reconciling persisted schedules"
  docker compose run --rm schedules ||
    fail "reset done, but the schedules were not reconciled; run make schedules"
  log "Done"
}

# --- dev -----------------------------------------------------------------------------

# Run `$1` with sh in a one-off pod: the I/O worker's pod template (image, environment,
# service account, staging writer identity), its worker container only, under a label no
# Service selects, with `reset.py` in the environment.
dev_helper() {
  "${K[@]}" delete pod "$HELPER_POD" --ignore-not-found --wait >/dev/null
  "${K[@]}" get "$WORKER_IO" -o json | jq \
    --arg name "$HELPER_POD" --arg steps "$1" --arg code "$RESET_PY" '
    .spec.template as $t | {
      apiVersion: "v1",
      kind: "Pod",
      metadata: {name: $name, labels: {app: "curate-d", component: "reset"}},
      spec: ($t.spec | del(.initContainers) | .restartPolicy = "Never" | .containers = [
        .containers[] | select(.name == "worker")
        | del(.ports, .livenessProbe, .readinessProbe, .startupProbe, .args)
        | .command = ["sh", "-c", $steps]
        | .env += [{name: "RESET_PY", value: $code}]
      ])
    }' | "${K[@]}" apply -f - >/dev/null
  local phase deadline=$((SECONDS + 900))
  while :; do
    phase=$("${K[@]}" get pod "$HELPER_POD" -o jsonpath='{.status.phase}')
    [[ $phase == Succeeded || $phase == Failed ]] && break
    ((SECONDS < deadline)) || { phase=Timeout && break; }
    sleep 3
  done
  "${K[@]}" logs "$HELPER_POD" | grep -v '^{' || true
  "${K[@]}" delete pod "$HELPER_POD" --wait >/dev/null
  [[ $phase == Succeeded ]]
}

dev_reset() {
  [[ -n $NAMESPACE ]] || fail "set CURATE_D_RESET_NAMESPACE to the namespace to reset"
  local environment
  environment=$("${K[@]}" get configmap "$CONFIGMAP" -o jsonpath='{.data.CURATE_D_ENVIRONMENT}')
  [[ $environment == dev ]] ||
    fail "namespace $NAMESPACE is configured for '$environment', not dev (context $(kubectl config current-context))"
  echo "context $(kubectl config current-context), namespace $NAMESPACE"

  log "What a reset removes"
  dev_helper "$PLAN_STEP" || fail "could not read the plan"
  $EXECUTE || { echo -e "\nDry run; add --execute to reset."; return; }
  confirm

  log "Scaling the API and workers to zero"
  local workload replicas
  for workload in "${APP_WORKLOADS[@]}"; do
    # A record left by a failed run holds the real count; the live one is 0 by now.
    replicas=$("${K[@]}" get "$workload" -o jsonpath="{.metadata.annotations.$RECORD}")
    [[ -n $replicas ]] || replicas=$("${K[@]}" get "$workload" -o jsonpath='{.spec.replicas}')
    "${K[@]}" annotate --overwrite "$workload" "$RECORD=$replicas"
    [[ -z $PAUSE_ANNOTATION ]] || "${K[@]}" annotate --overwrite "$workload" "$PAUSE_ANNOTATION=disabled"
  done
  "${K[@]}" scale --replicas=0 "${APP_WORKLOADS[@]}"
  local deadline=$((SECONDS + 600))
  while [[ -n $("${K[@]}" get pod -o name -l 'app=curate-d,component in (api,worker-io,worker-cpu)') ]]; do
    ((SECONDS < deadline)) || fail "pods still running after 10 minutes; the workloads stay at zero"
    sleep 5
  done

  log "Resetting"
  dev_helper "$RESET_STEPS" ||
    fail "reset failed; the API and workers stay at zero, paused (see the header)"

  log "Restarting the pooler, then the egress proxy (when configured)"
  if [[ -n $POOLER ]]; then
    "${K[@]}" rollout restart "$POOLER"
    "${K[@]}" rollout status "$POOLER" --timeout=5m
  fi
  [[ -z $EGRESS_PROXY ]] || "${K[@]}" rollout restart "$EGRESS_PROXY"

  log "Scaling the API and workers back"
  for workload in "${APP_WORKLOADS[@]}"; do
    replicas=$("${K[@]}" get "$workload" -o jsonpath="{.metadata.annotations.$RECORD}")
    "${K[@]}" scale --replicas="${replicas:-1}" "$workload"
  done
  for workload in ${EGRESS_PROXY:+"$EGRESS_PROXY"} "${APP_WORKLOADS[@]}"; do
    "${K[@]}" rollout status "$workload" --timeout=10m
  done
  "${K[@]}" annotate "${APP_WORKLOADS[@]}" ${PAUSE_ANNOTATION:+"$PAUSE_ANNOTATION-"} "$RECORD-"

  log "Reconciling persisted schedules"
  dev_helper "python -m curate_d.schedules reconcile" ||
    fail "reset done, but the schedules were not reconciled; rerun the deploy job's schedule step"
  log "Done"
}

case $ENVIRONMENT in
  local) local_reset ;;
  dev) dev_reset ;;
esac
