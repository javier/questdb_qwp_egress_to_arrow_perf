#!/usr/bin/env bash
# Runs ON the client box, detached by campaign.sh. Drives a whole campaign from inside
# the VPC so it survives the laptop going away (ssh drops, session ends, machine sleeps).
#
# Per engine: bring it up ALONE on the DB host -> optionally load it over the network ->
# measure it. Then generate the combined report and stop the databases.
#
# Env (all supplied by campaign.sh):
#   SERVER_PRIV  private IP of the DB host          ROWS       rows to read
#   READERS      reader sweep (default 1,2,4,8)     ENGINES    default all three
#   SKIP_LOAD=1  measure only - data already loaded (e.g. after an instance resize)
#   VARIANTS WARMUP REPEATS RUN_TIMEOUT             RIG_KEY    key to reach the DB host
#
# Completion marker: the final CAMPAIGN_DONE line.
set -uo pipefail

REPO=${REPO:-/opt/egress_perf}
: "${SERVER_PRIV:?SERVER_PRIV required}"
: "${ROWS:?ROWS required}"
READERS=${READERS:-1,2,4,8}
ENGINES=${ENGINES:-questdb clickhouse timescale}
RIG_KEY=${RIG_KEY:-/home/ubuntu/.ssh/rig.pem}
SSHS="ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=15 -i $RIG_KEY ubuntu@$SERVER_PRIV"

export DB_HOST="$SERVER_PRIV"
export VARIANTS="${VARIANTS:-}"
export WARMUP="${WARMUP:-2}"
export REPEATS="${REPEATS:-3}"
export SETTLE="${SETTLE:-10}"
export RUN_TIMEOUT="${RUN_TIMEOUT:-3600}"

cd "$REPO" || { echo "NO_REPO $REPO"; exit 1; }
# Clear only the engines we are about to run, so a single-engine re-run (e.g. re-measuring
# one engine with more warmup) composes with results already collected for the others.
for e in $ENGINES; do rm -f "results/run_${ROWS}_${e}.json"; done

echo "campaign: rows=$ROWS readers=$READERS engines='$ENGINES' variants='${VARIANTS:-all}'"
echo "          warmup=$WARMUP settle=${SETTLE}s repeats=$REPEATS run_timeout=$RUN_TIMEOUT skip_load=${SKIP_LOAD:-0} qdb_parquet=${QDB_PARQUET:-0}"

for eng in $ENGINES; do
    echo "==================== $eng ===================="
    date -u
    if ! $SSHS "cd $REPO && ./serve.sh up $eng"; then echo "SERVE_UP_FAILED $eng"; continue; fi
    df -h / | tail -1
    if [ "${SKIP_LOAD:-0}" != "1" ]; then
        if ! ./client.sh load "$eng" "$ROWS"; then echo "LOAD_FAILED $eng"; continue; fi
    else
        echo "-- skipping load (SKIP_LOAD=1), measuring existing data"
    fi
    if ! ./client.sh measure "$eng" "$ROWS" "$READERS"; then echo "MEASURE_FAILED $eng"; continue; fi
    echo "OK $eng"
done

echo "== generating combined report"
ISOLATED=1 ./client.sh report "$ROWS"
$SSHS "cd $REPO && ./serve.sh down"
date -u
echo "CAMPAIGN_DONE"
