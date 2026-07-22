#!/usr/bin/env bash
#
# DB-HOST helper for the split setup (databases + ingestion here; queries from another
# instance via client.sh). Brings engines up/down and loads data. You coordinate which
# engine is up: to isolate, keep just one running while the client measures it.
#
# Usage (on the DB host):
#   ./serve.sh up <engine|all>       start engine(s); `up <engine>` stops the others
#   ./serve.sh load <engine> <rows>  (re)load rows into an engine (drop + recreate)
#   ./serve.sh down                  stop all engines
#
# The DB host must publish 9000/8812/9009 (QuestDB), 8123/9001 (ClickHouse),
# 5432 (Timescale) to the client instance (security group / firewall).
#
# Env: PYTHON — host interpreter with the clients for loading; else the egress_bench
#   image (docker compose build bench) is used.
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

ALL="questdb clickhouse timescale"
svc_of()   { case "$1" in questdb) echo questdb;; clickhouse) echo clickhouse;; timescale) echo timescaledb;; esac; }
cname_of() { case "$1" in questdb) echo egress_questdb;; clickhouse) echo egress_clickhouse;; timescale) echo egress_timescale;; esac; }

if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON:-/nonexistent}" ]; then
  RUN() { "$PYTHON" "$@"; }
else
  RUN() { docker compose run --rm bench python "$@"; }
fi

wait_healthy() {
  local c="$1"
  for _ in $(seq 1 90); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)" = "healthy" ] && return 0
    sleep 2
  done
  echo "ERROR: $c did not become healthy" >&2; return 1
}

cmd="${1:-}"
case "$cmd" in
  up)
    what="${2:?engine|all}"
    if [ "$what" = "all" ]; then
      docker compose up -d questdb clickhouse timescaledb
      for e in $ALL; do wait_healthy "$(cname_of "$e")"; done
    else
      for other in $ALL; do
        [ "$other" = "$what" ] && continue
        docker compose stop "$(svc_of "$other")" >/dev/null 2>&1 || true
      done
      docker compose up -d "$(svc_of "$what")"
      wait_healthy "$(cname_of "$what")"
      echo "==> only '$what' is running; point the client at this host and measure it."
    fi
    ;;
  load)
    engine="${2:?engine}"; rows="${3:?rows}"
    RUN "load_${engine}.py" --rows "$rows" --recreate
    ;;
  down)
    docker compose stop questdb clickhouse timescaledb
    ;;
  *)
    echo "usage: ./serve.sh up <engine|all> | load <engine> <rows> | down" >&2
    exit 2
    ;;
esac
