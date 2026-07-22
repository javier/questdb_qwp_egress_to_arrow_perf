#!/usr/bin/env bash
#
# Tear down the benchmark stack.
#   ./teardown.sh            stop containers + delete volumes (all loaded data)
#   ./teardown.sh --images   also remove the DB images AND the built client image
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

RMI=""
if [ "${1:-}" = "--images" ]; then RMI="--rmi all"; fi

echo "==> Stopping containers and removing volumes ..."
# --profile client so the built bench client image/container is included too.
docker compose --profile client down -v $RMI

if [ -n "$RMI" ]; then
  echo "==> Removed containers, volumes, DB images, and the built client image."
else
  echo "==> Removed containers and volumes. (Pass --images to also delete all images.)"
fi
