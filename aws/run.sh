#!/usr/bin/env bash
# Drive the full split benchmark from the laptop, fully automated:
#   for each engine -> bring it up ALONE on the server (isolation) -> load it from the
#   client over the network -> measure it from the client (warmup + repeat-average);
#   then the client generates the combined report and we scp RESULTS.md back locally.
#
#   ./run.sh [ROWS] [READERS]     ROWS default 50000000, READERS default 1,2,4,8
#   Env: WARMUP(2) REPEATS(3)
#        VARIANTS  restrict read paths, e.g. VARIANTS=qwp-arrow,arrow,adbc — recommended
#                  for very large campaigns: those stream (constant client RAM), while
#                  clickhouse/native and timescale/connectorx buffer the whole result.
set -euo pipefail
cd "$(dirname "$0")"
. ./env.sh
. ./lib.sh
egb_ensure_auth

ROWS="${1:-50000000}"
READERS="${2:-1,2,4,8}"
WARMUP="${WARMUP:-2}"
REPEATS="${REPEATS:-3}"
ENGINES="questdb clickhouse timescale"

SERVER_PRIV=$(egb_private_ip "$(egb_instance_id server)")
echo "== server private ip: $SERVER_PRIV  (client reaches DBs here)"
R="cd ${EGB_REMOTE_DIR} &&"

for eng in $ENGINES; do
    echo
    echo "==================== $eng ===================="
    echo "-- server: bring up ONLY $eng (others stopped)"
    egb_ssh server "$R ./serve.sh up $eng"
    echo "-- client: load $ROWS rows into $eng over the network"
    egb_ssh client "$R DB_HOST=$SERVER_PRIV ./client.sh load $eng $ROWS"
    echo "-- client: measure $eng (readers=$READERS warmup=$WARMUP repeats=$REPEATS${VARIANTS:+ variants=$VARIANTS})"
    egb_ssh client "$R DB_HOST=$SERVER_PRIV WARMUP=$WARMUP REPEATS=$REPEATS VARIANTS='${VARIANTS:-}' RUN_TIMEOUT='${RUN_TIMEOUT:-}' ./client.sh measure $eng $ROWS $READERS"
done

echo
echo "== client: generate combined report"
egb_ssh client "$R WARMUP=$WARMUP REPEATS=$REPEATS ISOLATED=1 ./client.sh report $ROWS"

echo "== stop the databases on the server"
egb_ssh server "$R ./serve.sh down" || true

mkdir -p "${EGB_REPO_DIR}/results/aws"
LOCAL="${EGB_REPO_DIR}/results/aws/RESULTS.md"
egb_scp_from client "${EGB_REMOTE_DIR}/RESULTS.md" "$LOCAL"
echo
echo "== DONE. Report copied to $LOCAL"
echo "   (tear the rig down with ./teardown.sh when finished)"
