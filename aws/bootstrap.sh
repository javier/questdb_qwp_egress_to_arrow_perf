#!/usr/bin/env bash
# Bootstrap both boxes: push the repo, install Docker, pre-pull the DB images on the
# server, and build the client image (compiles the QuestDB 5.0 client) on the client.
# The client build is the only heavy step, and it only happens on the client box.
set -euo pipefail
cd "$(dirname "$0")"
. ./env.sh
. ./lib.sh
egb_ensure_auth

echo "== wait for ssh on both boxes"
egb_wait_ssh server
egb_wait_ssh client

echo "== push repo -> both boxes ($EGB_REMOTE_DIR)"
egb_ssh server "sudo mkdir -p ${EGB_REMOTE_DIR} && sudo chown ${EGB_SSH_USER} ${EGB_REMOTE_DIR}"
egb_ssh client "sudo mkdir -p ${EGB_REMOTE_DIR} && sudo chown ${EGB_SSH_USER} ${EGB_REMOTE_DIR}"
egb_push_repo server
egb_push_repo client

echo "== install docker (server)"
egb_ssh server "bash ${EGB_REMOTE_DIR}/aws/box_bootstrap.sh"
echo "== install docker (client)"
egb_ssh client "bash ${EGB_REMOTE_DIR}/aws/box_bootstrap.sh"

echo "== pre-pull DB images (server)"
egb_ssh server "cd ${EGB_REMOTE_DIR} && docker compose pull questdb clickhouse timescaledb"

echo "== build client image (client) — compiles the QuestDB 5.0 client, a few minutes"
egb_ssh client "cd ${EGB_REMOTE_DIR} && docker compose build bench"

echo "== bootstrap done. Next: ./run.sh <rows> [readers]"
