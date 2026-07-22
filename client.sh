#!/usr/bin/env bash
#
# Run the benchmark CLIENT by itself, against databases on ANOTHER machine.
#
# The client only READS and writes the report - it never starts/stops containers.
# In a split setup you bring ONE engine up at a time on the DB host (see serve.sh),
# point the client at it, measure, then switch engines on the host and measure again.
# When you've measured every engine, generate the combined report.
#
# Usage (on the query instance):
#   DB_HOST=<db-host-or-ip> ./client.sh measure <engine|all> <rows> [readers]
#   ./client.sh report <rows>
#
#   measure  runs warmup+repeat reads against whatever engine is currently reachable
#            and saves results/run_<rows>_<engine>.json
#   report   merges all saved results/run_<rows>_*.json into one combined section
#            appended to RESULTS.md  (this is the client generating the report)
#
# Endpoints come from DB_HOST (override any individually):
#   QDB_ADDR=$DB_HOST:9000  CH_HOST=$DB_HOST  CH_HTTP_PORT=8123  CH_NATIVE_PORT=9001
#   CH_PASSWORD=bench       TS_HOST=$DB_HOST  TS_PORT=5432
# The DB host must publish those ports to this instance (security group / firewall).
#
# Env:
#   WARMUP(2) REPEATS(3) ISOLATED(unset) — set ISOLATED=1 if you brought engines up one
#     at a time on the host (adds the isolation note to the report).
#   VARIANTS — restrict which read paths run, e.g. VARIANTS=qwp-arrow,arrow,adbc for the
#     streaming-only set (constant client memory; the buffering variants need a big-RAM
#     client at high row counts).
#   RUN_TIMEOUT — per-run cap in seconds passed to compare.py (default there is 180s).
#     Raise it for large row counts or slow paths, or cells get marked TIMEOUT.
#   PYTHON — host interpreter with the clients; if unset, uses the egress_bench image
#     via `docker run` (build it first with `docker compose build bench`; use a
#     reachable DB_HOST, not localhost, in docker mode).
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

DB_HOST="${DB_HOST:-localhost}"
export QDB_ADDR="${QDB_ADDR:-$DB_HOST:9000}"
export CH_HOST="${CH_HOST:-$DB_HOST}"
export CH_HTTP_PORT="${CH_HTTP_PORT:-8123}"
export CH_NATIVE_PORT="${CH_NATIVE_PORT:-9001}"
export CH_PASSWORD="${CH_PASSWORD:-bench}"
export TS_HOST="${TS_HOST:-$DB_HOST}"
export TS_PORT="${TS_PORT:-5432}"
WARMUP="${WARMUP:-2}"
REPEATS="${REPEATS:-3}"

if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON:-/nonexistent}" ]; then
  RUN() { "$PYTHON" "$@"; }
else
  ENVS=(-e QDB_ADDR -e CH_HOST -e CH_HTTP_PORT -e CH_NATIVE_PORT -e CH_PASSWORD -e TS_HOST -e TS_PORT)
  RUN() { docker run --rm -v "$HERE:/app" -w /app "${ENVS[@]}" egress_bench:latest python "$@"; }
fi

mkdir -p results
cmd="${1:-}"
case "$cmd" in
  load)
    engine="${2:?engine: questdb | clickhouse | timescale}"
    rows="${3:?rows}"
    echo "==> loading $rows rows into '$engine' @ DB_HOST=$DB_HOST (drop + recreate)"
    RUN "load_${engine}.py" --rows "$rows" --recreate
    ;;
  measure)
    engine="${2:?engine: questdb | clickhouse | timescale | all}"
    rows="${3:?rows (the --limit to read)}"
    readers="${4:-1,2,4,8}"
    engs="$engine"; [ "$engine" = "all" ] && engs="questdb,clickhouse,timescale"
    vflag=(); [ -n "${VARIANTS:-}" ] && vflag=(--variants "$VARIANTS")
    # Raise this for big campaigns: one 500M adbc run at 1 reader takes ~250s, well past
    # compare.py's 180s default, which would mark every such cell TIMEOUT.
    [ -n "${RUN_TIMEOUT:-}" ] && vflag+=(--run-timeout "$RUN_TIMEOUT")
    echo "==> measuring '$engine' @ DB_HOST=$DB_HOST (limit=$rows, readers=$readers, warmup=$WARMUP, repeats=$REPEATS${VARIANTS:+, variants=$VARIANTS})"
    RUN compare.py --limit "$rows" --readers "$readers" --engines "$engs" \
        --warmup "$WARMUP" --repeats "$REPEATS" "${vflag[@]}" --out "results/run_${rows}_${engine}"
    echo "==> saved results/run_${rows}_${engine}.json"
    echo "    switch the engine on the DB host and measure again, then: ./client.sh report $rows"
    ;;
  report)
    rows="${2:?rows}"
    iso=(); [ -n "${ISOLATED:-}" ] && iso=(--isolated)
    RUN merge_results.py --inputs "results/run_${rows}_*.json" --rows "$rows" \
        --warmup "$WARMUP" --repeats "$REPEATS" "${iso[@]}" --append RESULTS.md
    ;;
  *)
    echo "usage: DB_HOST=<host> ./client.sh load    <engine> <rows>" >&2
    echo "       DB_HOST=<host> ./client.sh measure <engine|all> <rows> [readers]" >&2
    echo "                      ./client.sh report  <rows>" >&2
    exit 2
    ;;
esac
