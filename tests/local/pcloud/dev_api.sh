#!/usr/bin/env bash
# Exercise a deployed control plane by hand: curl for the API, kubectl for what has no
# endpoint (uploading test media, listing a pCloud folder, starting a scan or a schedule).
#
# Every test file lives under $CURATE_D_TEST_ROOT in the connected pCloud account:
#   <root>/source      the fixture library, scanned
#   <root>/organized   the policy's only destination
#
# Requires CURATE_D_URL, CURATE_D_NAMESPACE and CURATE_D_TEST_ROOT (see .env.example). The I/O
# worker pod is found by its labels (app=curate-d, component=worker-io).
#
# Usage: tests/local/pcloud/dev_api.sh COMMAND [ARGS]   (run with no command for the list)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
# Export the KEY=VALUE lines of $1, skipping comments and keys the environment already sets.
# Values are taken literally, one pair of surrounding quotes stripped, never evaluated.
load_env() {
  local line key value skip='^[[:space:]]*(#|$)' name='^[A-Za-z_][A-Za-z0-9_]*$'
  local quoted='^"(.*)"$' single="^'(.*)'$"
  while IFS= read -r line; do
    if [[ $line =~ $skip || $line != *=* ]]; then continue; fi
    key=${line%%=*} value=${line#*=}
    if [[ ! $key =~ $name ]]; then continue; fi
    if [[ $value =~ $quoted || $value =~ $single ]]; then value=${BASH_REMATCH[1]}; fi
    if [[ -z ${!key+x} ]]; then export "$key=$value"; fi
  done <<<"$1"
}
# Deployment-specific values come from the gitignored .env (see .env.example), which may be
# a 1Password Environment mounted as a named pipe: not a regular file (-f fails), served
# afresh on every read, and empty to bash 3.2's `source`. So read it once, whole, and parse it.
if [[ -e $REPO/.env ]]; then load_env "$(<"$REPO/.env")"; fi
BASE=${CURATE_D_URL:?set CURATE_D_URL (the deployed control plane URL) in .env}
NAMESPACE=${CURATE_D_NAMESPACE:?set CURATE_D_NAMESPACE (the Kubernetes namespace) in .env}
TEST_ROOT=${CURATE_D_TEST_ROOT:?set CURATE_D_TEST_ROOT (the pCloud test folder) in .env}
# A policy refuses a relative destination root, and scans and reports match absolute paths.
[[ $TEST_ROOT == /* ]] || { echo "CURATE_D_TEST_ROOT must be absolute: /$TEST_ROOT, not $TEST_ROOT" >&2; exit 1; }
SOURCE=$TEST_ROOT/source
ORGANIZED=$TEST_ROOT/organized
DESTINATION=test-organized
POD=
STATE=${XDG_CACHE_HOME:-$HOME/.cache}/curate-d-local
JAR=$STATE/cookies.txt
mkdir -p "$STATE"

# curl that fails on HTTP errors but still prints the problem body.
api() {
  local method=$1 path=$2
  shift 2
  curl -sS --fail-with-body -X "$method" "$BASE$path" -H 'Accept: application/json' "$@" | jq .
}
json() { api "$1" "$2" -H 'Content-Type: application/json' --data-binary "$3"; }

# The connected pCloud account's id; call it as `acct=$(account)` on its own, so that its
# failure stops the script (set -e ignores a failing substitution inside an argument).
# Either $CURATE_D_ACCOUNT or the only connection.
account() {
  if [[ -n ${CURATE_D_ACCOUNT:-} ]]; then echo "$CURATE_D_ACCOUNT"; return; fi
  local accounts
  accounts=$(curl -sS --fail-with-body "$BASE/connections" | jq -r '.[] | select(.provider == "pcloud") | .account')
  case $(grep -c . <<<"$accounts") in
    0) echo "no pCloud connection; run: $0 connect" >&2; exit 1 ;;
    1) echo "$accounts" ;;
    *) echo "several pCloud connections; set CURATE_D_ACCOUNT to one of: $accounts" >&2; exit 1 ;;
  esac
}

# Set POD to the I/O worker's running pod, found once by the worker-io StatefulSet's labels.
find_io_pod() {
  [[ -z $POD ]] || return 0
  POD=$(kubectl -n "$NAMESPACE" get pods -l app=curate-d,component=worker-io \
    --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}') || exit 1
  POD=${POD%% *}
  [[ -n $POD ]] || { echo "no running I/O worker pod (component=worker-io) in $NAMESPACE" >&2; exit 1; }
}

in_pod() {
  find_io_pod
  kubectl -n "$NAMESPACE" exec -i "$POD" -c worker -- python - "$@" <"$HERE/in_pod.py"
}

policy_document() {
  jq -n --arg name "$DESTINATION" --arg account "$1" --arg root "$ORGANIZED" '{
    destinations: [{name: $name, provider: "pcloud", account: $account, root_path: $root}],
    rules: [
      {name: "photos", when: {category: "Photos"}, destinations: [$name]},
      {name: "documents", when: {category: "Documents"}, destinations: [$name]},
      {name: "audio", when: {category: "Audio"}, destinations: [$name]}
    ],
    retention: []
  }'
}

usage() {
  cat <<EOF
$BASE  (namespace $NAMESPACE, test root $TEST_ROOT)

  health                      liveness and readiness
  openapi [FILE]              the OpenAPI document (also browsable at $BASE/docs)
  connect                     authorize a pCloud account (curl starts, a browser consents)
  connections                 every connection and its health
  seed [TAG]                  generate the fixture library and upload it to $SOURCE;
                              create $ORGANIZED
  tree [PATH]                 files under PATH in pCloud (default $TEST_ROOT)
  taxonomy | templates | policy
                              the configuration in force
  history KIND                taxonomy, templates or policy versions
  set-policy                  only destination $ORGANIZED; photos, documents, audio; no release
  dry-run                     start a dry run against the policy, wait, print its export
  dry-run-status RUN_ID | dry-run-export RUN_ID
  scan                        enqueue one scan of $SOURCE
  reconcile                   run the reconcile schedule now
  report                      each file under $SOURCE: classification, review, placements
  jobs | failures             progress per stage; failed units
  review                      open review items
  item ID                     one review item
  resolve ID FACTS_JSON       e.g. '{"media_kind":"document","category":"Documents","subcategory":"Finance"}'
  promote ID [VALUES_JSON]    add the item's rejected values to the taxonomy (all when omitted)
  exclude ID REASON           keep the item's content out of organization
  content ID                  a content object's audit history
  throughput                  queue limits per stage
EOF
}

command=${1:-}
shift || true
case $command in
  health)
    api GET /health/live
    api GET /health/ready ;;
  openapi)
    if [[ -n ${1:-} ]]; then curl -sS --fail-with-body "$BASE/openapi.json" -o "$1" && echo "$1"
    else api GET /openapi.json; fi ;;
  connect)
    # The callback must carry the cookie set when the authorization started. curl holds
    # that cookie, not the browser, so the browser's first landing on the callback is
    # refused without spending the code; paste its URL here to finish with the cookie.
    redirect=$(curl -sS --fail-with-body -c "$JAR" -X POST "$BASE/connections/pcloud/authorizations" | jq -r .redirect_url)
    echo "Open this in a browser and allow access:"
    echo "  $redirect"
    echo "It lands on $BASE/connections/pcloud/callback?... and is refused there (no cookie)."
    read -r -p "Paste that callback URL: " callback
    [[ $callback == "$BASE/connections/pcloud/callback?"* ]] || { echo "not a callback URL for $BASE" >&2; exit 1; }
    curl -sS --fail-with-body -b "$JAR" "$callback" | jq . ;;
  connections) api GET /connections ;;
  seed)
    acct=$(account)
    library=$(mktemp -d)/curate-d-library
    (cd "$REPO" && uv run python -m tests.local.pcloud.write_library "$library" ${1:+"$1"})
    find_io_pod
    kubectl -n "$NAMESPACE" exec -i "$POD" -c worker -- python -c '
import shutil, sys, tarfile
shutil.rmtree("/scratch/curate-d-seed", ignore_errors=True)
tarfile.open(fileobj=sys.stdin.buffer, mode="r|").extractall("/scratch/curate-d-seed", filter="data")
' < <(COPYFILE_DISABLE=1 tar -C "$library" -cf - .)
    in_pod seed "$acct" "$SOURCE" "$ORGANIZED" ;;
  tree) acct=$(account) && in_pod tree "$acct" "${1:-$TEST_ROOT}" ;;
  taxonomy | templates | policy) api GET "/configuration/$command" ;;
  history) api GET "/configuration/${1:?KIND}/history" ;;
  set-policy) acct=$(account) && json PUT /configuration/policy "$(policy_document "$acct")" ;;
  dry-run)
    run=$(curl -sS --fail-with-body -X POST "$BASE/dry-runs" -H 'Content-Type: application/json' -d '{}' | jq -r .run_id)
    echo "run $run" >&2
    while :; do
      state=$(curl -sS --fail-with-body "$BASE/dry-runs/$run" | jq -r .state)
      echo "  $state" >&2
      [[ $state == queued || $state == running ]] || break
      sleep 5
    done
    api GET "/dry-runs/$run/export" ;;
  dry-run-status) api GET "/dry-runs/${1:?RUN_ID}" ;;
  dry-run-export) api GET "/dry-runs/${1:?RUN_ID}/export" ;;
  scan) acct=$(account) && in_pod scan "$acct" "$SOURCE" ;;
  reconcile) in_pod trigger reconcile ;;
  report) acct=$(account) && in_pod report "$acct" "$SOURCE" ;;
  jobs) api GET /jobs ;;
  failures) api GET /jobs/failures ;;
  review) api GET /review ;;
  item) api GET "/review/${1:?ID}" ;;
  resolve) json POST "/review/${1:?ID}/facts" "${2:?FACTS_JSON}" ;;
  promote) json POST "/review/${1:?ID}/taxonomy" "{\"values\": ${2:-[]}}" ;;
  exclude) json POST "/review/${1:?ID}/exclusion" "$(jq -n --arg r "${2:?REASON}" '{reason: $r}')" ;;
  content) api GET "/content/${1:?ID}/history" ;;
  throughput) api GET /throughput ;;
  *) usage ;;
esac
