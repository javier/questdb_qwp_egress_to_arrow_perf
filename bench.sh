#!/usr/bin/env bash
#
# One-command egress benchmark with PER-ENGINE ISOLATION.
#
# For each engine in turn: stop the other two DB containers (freeing their RAM/CPU/
# disk), start only this engine, (re)load ROWS rows, then measure its read paths with
# warmup + repeat-and-average - so every number is taken with a single database and a
# single client competing for the box. Results from the three isolated passes are
# merged into one combined, machine-tagged section appended to RESULTS.md.
#
# Usage:
#   ./bench.sh [ROWS] [READERS]
#     ROWS     rows to load+read per engine        (default 10000000)
#     READERS  comma list of connection counts     (default 1,2,4,8)
# Env:
#   WARMUP   unmeasured warmup runs per cell        (default 2)
#   REPEATS  measured runs per cell (mean reported) (default 3)
#   VARIANTS restrict read paths, e.g. VARIANTS=qwp-arrow,arrow,adbc (streaming only;
#            the buffering variants need a big-RAM client at high row counts)
#   PYTHON   host interpreter with the QuestDB 5.0 client + read clients. If set, runs
#            on the host; if UNSET, the client runs in the Docker image that
#            docker compose builds from Dockerfile.client (the reproducible default).
#
set -euo pipefail

ROWS="${1:-10000000}"
READERS="${2:-${READERS:-1,2,4,8}}"
WARMUP="${WARMUP:-2}"
REPEATS="${REPEATS:-3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ENGINES="questdb clickhouse timescale"
svc_of()   { case "$1" in questdb) echo questdb;; clickhouse) echo clickhouse;; timescale) echo timescaledb;; esac; }
cname_of() { case "$1" in questdb) echo egress_questdb;; clickhouse) echo egress_clickhouse;; timescale) echo egress_timescale;; esac; }

# --- client mode: host interpreter, or the compose-built client image ------------
if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON:-/nonexistent}" ]; then
  echo "==> Client: host interpreter $PYTHON"
  RUN() { "$PYTHON" "$@"; }
else
  echo "==> Client: docker image (building QuestDB 5.0 client if needed; first time is slow) ..."
  docker compose build bench
  RUN() { docker compose run --rm bench python "$@"; }
fi

wait_healthy() {
  local c="$1"
  for _ in $(seq 1 90); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)" = "healthy" ]; then
      return 0
    fi
    sleep 2
  done
  echo "ERROR: $c did not become healthy" >&2
  return 1
}

mkdir -p results
rm -f "results/run_${ROWS}"_*.json "results/run_${ROWS}"_*.md

for eng in $ENGINES; do
  csvc="$(svc_of "$eng")"; cname="$(cname_of "$eng")"
  echo
  echo "==================== $eng (isolated) ===================="

  echo "--> Stopping the other engines so only $eng holds resources ..."
  for other in $ENGINES; do
    [ "$other" = "$eng" ] && continue
    docker compose stop "$(svc_of "$other")" >/dev/null 2>&1 || true
  done
  echo "--> Starting $csvc ..."
  docker compose up -d "$csvc"
  wait_healthy "$cname"

  echo "--> Loading $ROWS rows into $eng (drop + recreate) ..."
  LFLAG=()
  # QuestDB only: store partitions as Parquet instead of native columns. Spelled as an
  # `if` rather than an && chain because `set -e` kills the script on a failing AND-list.
  if [ "$eng" = "questdb" ] && [ "${QDB_PARQUET:-0}" = "1" ]; then
    LFLAG=(--parquet)
  fi
  RUN "load_${eng}.py" --rows "$ROWS" --recreate "${LFLAG[@]}"

  echo "--> Measuring $eng (readers=$READERS, warmup=$WARMUP, repeats=$REPEATS) ..."
  VFLAG=(); [ -n "${VARIANTS:-}" ] && VFLAG=(--variants "$VARIANTS")
  [ -n "${SETTLE:-}" ] && VFLAG+=(--settle "$SETTLE")
  [ -n "${RUN_TIMEOUT:-}" ] && VFLAG+=(--run-timeout "$RUN_TIMEOUT")
  RUN compare.py --limit "$ROWS" --readers "$READERS" --engines "$eng" \
      --warmup "$WARMUP" --repeats "$REPEATS" "${VFLAG[@]}" --out "results/run_${ROWS}_${eng}"
done

echo
echo "==> Merging the three isolated passes into RESULTS.md ..."
RUN merge_results.py --inputs "results/run_${ROWS}_*.json" --rows "$ROWS" \
    --warmup "$WARMUP" --repeats "$REPEATS" --isolated --append RESULTS.md

echo "==> Done. Combined result for $ROWS rows appended to RESULTS.md"
