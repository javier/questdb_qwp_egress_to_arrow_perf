#!/usr/bin/env bash
# UNATTENDED campaign driver — launch once, walk away.
#
# Ships the current code to both boxes, launches the campaign DETACHED on the client
# (so it survives your laptop, your ssh session, and this script), waits for it, then
# pulls the results back. Keeps the rig running by default.
#
#   ./campaign.sh [ROWS] [READERS]
#
# Run it detached so it also survives your terminal:
#   nohup ./campaign.sh 500000000 1,2,4,8 > /tmp/campaign_driver.log 2>&1 &
#
# Env:
#   VARIANTS      restrict read paths, e.g. qwp-arrow,arrow,adbc (streaming only)
#   WARMUP(1) REPEATS(3) RUN_TIMEOUT(3600)
#   SKIP_LOAD=1   measure only — data is already loaded (e.g. after an instance resize)
#   ENGINES       default "questdb clickhouse timescale"
#   DEADMAN_MIN   schedule an OS shutdown on both boxes after N minutes as a runaway-cost
#                 backstop (default 720 = 12h; 0 disables). By default this STOPS the
#                 instances, which PRESERVES all data and installed software.
#   DEADMAN_TERMINATE=1  make that shutdown terminate instead — DESTROYS ALL DATA. Off by default.
#   TEARDOWN=1    tear the rig down when finished (default 0 = leave it up)
#   SERVER_PUB / CLIENT_PUB / SERVER_PRIV
#                 skip AWS lookups and use these IPs (handy when your AWS session expired
#                 but ssh still works — the campaign itself needs no AWS credentials)
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh
. ./lib.sh

ROWS="${1:-50000000}"
READERS="${2:-1,2,4,8}"
DEADMAN_MIN="${DEADMAN_MIN:-720}"
LOG="${LOG:-/tmp/egress_campaign.log}"
say() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$LOG"; }

say "START campaign rows=$ROWS readers=$READERS variants='${VARIANTS:-all}' skip_load=${SKIP_LOAD:-0}"

# --- resolve boxes (env override avoids needing AWS credentials at all) ----------------
if [ -z "${SERVER_PUB:-}" ] || [ -z "${CLIENT_PUB:-}" ] || [ -z "${SERVER_PRIV:-}" ]; then
    egb_ensure_auth
    SERVER_ID=$(egb_instance_id server); CLIENT_ID=$(egb_instance_id client)
    [ -n "$SERVER_ID" ] && [ -n "$CLIENT_ID" ] || { say "ERROR: rig instances not found"; exit 1; }
    SERVER_PUB=$(egb_public_ip "$SERVER_ID"); CLIENT_PUB=$(egb_public_ip "$CLIENT_ID")
    SERVER_PRIV=$(egb_private_ip "$SERVER_ID")
fi
say "server pub=$SERVER_PUB priv=$SERVER_PRIV | client pub=$CLIENT_PUB"

SSHO="${EGB_SSH_OPTS[*]}"
rssh() { ssh ${SSHO} "ubuntu@$1" "$2"; }

# --- cost backstop: stop (not destroy) the boxes if something hangs --------------------
if [ "$DEADMAN_MIN" != "0" ]; then
    if [ "${DEADMAN_TERMINATE:-0}" = "1" ]; then
        egb_ensure_auth
        for iid in "${SERVER_ID:-}" "${CLIENT_ID:-}"; do
            [ -n "$iid" ] && aws ec2 modify-instance-attribute --instance-id "$iid" \
                --instance-initiated-shutdown-behavior Value=terminate >>"$LOG" 2>&1
        done
        say "WARNING: dead-man set to TERMINATE — data will be destroyed if it fires"
    fi
    for h in "$SERVER_PUB" "$CLIENT_PUB"; do
        rssh "$h" "sudo shutdown -c; sudo shutdown -h +${DEADMAN_MIN}" >>"$LOG" 2>&1
    done
    say "dead-man armed at +${DEADMAN_MIN} min (stop = data preserved unless DEADMAN_TERMINATE=1)"
fi

# --- is a campaign already in flight? (idempotent — never launch twice) ----------------
STATE=fresh
if rssh "$CLIENT_PUB" "grep -q CAMPAIGN_DONE /tmp/campaign.log" 2>/dev/null; then STATE=done
elif rssh "$CLIENT_PUB" "pgrep -f '[b]ox_campaign' > /dev/null" 2>/dev/null; then STATE=running; fi
say "detected state: $STATE"

if [ "$STATE" = "fresh" ]; then
    for role in server client; do egb_push_repo "$role" >>"$LOG" 2>&1; done
    scp ${SSHO} "$EGB_KEY_FILE" "ubuntu@$CLIENT_PUB:/home/ubuntu/.ssh/rig.pem" >>"$LOG" 2>&1
    rssh "$CLIENT_PUB" "chmod 600 /home/ubuntu/.ssh/rig.pem" >>"$LOG" 2>&1
    say "code + rig key delivered"

    ENVS="SERVER_PRIV=$SERVER_PRIV ROWS=$ROWS READERS=$READERS"
    ENVS="$ENVS VARIANTS='${VARIANTS:-}' WARMUP='${WARMUP:-1}' REPEATS='${REPEATS:-3}'"
    ENVS="$ENVS RUN_TIMEOUT='${RUN_TIMEOUT:-3600}' SKIP_LOAD='${SKIP_LOAD:-0}'"
    [ -n "${ENGINES:-}" ] && ENVS="$ENVS ENGINES='$ENGINES'"
    rssh "$CLIENT_PUB" "rm -f /tmp/campaign.log; nohup env $ENVS bash ${EGB_REMOTE_DIR}/aws/box_campaign.sh > /tmp/campaign.log 2>&1 & echo launched" >>"$LOG" 2>&1
    say "campaign launched detached on client"
fi

# --- wait ------------------------------------------------------------------------------
DONE=no
for i in $(seq 1 400); do
    if rssh "$CLIENT_PUB" "grep -q CAMPAIGN_DONE /tmp/campaign.log" 2>/dev/null; then
        DONE=yes; say "CAMPAIGN_DONE after ~${i} min"; break
    fi
    if ! rssh "$CLIENT_PUB" "pgrep -f '[b]ox_campaign' > /dev/null" 2>/dev/null; then
        say "orchestrator gone without CAMPAIGN_DONE (~${i} min)"; break
    fi
    [ $((i % 10)) -eq 0 ] && say "still running (~${i} min)"
    sleep 60
done

# --- fetch whatever exists --------------------------------------------------------------
OUT="${EGB_REPO_DIR}/results/aws"; mkdir -p "$OUT"
scp ${SSHO} "ubuntu@$CLIENT_PUB:/tmp/campaign.log" "$OUT/campaign_${ROWS}.log" >>"$LOG" 2>&1
scp ${SSHO} "ubuntu@$CLIENT_PUB:${EGB_REMOTE_DIR}/RESULTS.md" "$OUT/RESULTS.md" >>"$LOG" 2>&1
scp ${SSHO} "ubuntu@$CLIENT_PUB:${EGB_REMOTE_DIR}/results/run_${ROWS}_*.json" "$OUT/" >>"$LOG" 2>&1
say "results fetched to $OUT"

if [ "${TEARDOWN:-0}" = "1" ]; then
    say "tearing down"; bash ./teardown.sh >>"$LOG" 2>&1; say "teardown complete"
else
    say "rig left running (TEARDOWN=1 to remove it)"
fi
say "END done=$DONE"
