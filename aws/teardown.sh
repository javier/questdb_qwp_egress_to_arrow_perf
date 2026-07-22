#!/usr/bin/env bash
# Tear down everything provision.sh created, then audit by tag. Safe to re-run.
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh
egb_ensure_auth

SG_NAME="${EGB_PREFIX}-sg"
PG_NAME="${EGB_PREFIX}-pg"

echo "== terminate instances"
IDS=$(aws ec2 describe-instances \
    --filters "Name=tag:${EGB_TAG_KEY},Values=${EGB_TAG_VAL}" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
if [ -n "$IDS" ]; then
    aws ec2 terminate-instances --instance-ids $IDS >/dev/null
    echo "   waiting for: $IDS"
    aws ec2 wait instance-terminated --instance-ids $IDS
fi

echo "== security group (ENIs can linger briefly after terminate)"
SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
    for _ in $(seq 1 12); do
        aws ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null && break
        sleep 10
    done
fi

echo "== placement group"
aws ec2 delete-placement-group --group-name "$PG_NAME" 2>/dev/null

echo "== key pair"
# ONLY a key this rig created (marker present) is ever deleted. A reused / pre-existing
# key is never touched — there is no override that can delete it.
if [ -f "$EGB_KEY_CREATED_MARKER" ]; then
    echo "   '$EGB_KEY_NAME' was created by provision.sh — deleting it and its local .pem"
    aws ec2 delete-key-pair --key-name "$EGB_KEY_NAME" 2>/dev/null
    rm -f "$EGB_KEY_FILE" "$EGB_KEY_CREATED_MARKER"
else
    echo "   '$EGB_KEY_NAME' kept (not created by this rig — never deleted)"
fi

echo "== tag audit (should list nothing)"
aws resourcegroupstaggingapi get-resources \
    --tag-filters "Key=${EGB_TAG_KEY},Values=${EGB_TAG_VAL}" \
    --query 'ResourceTagMappingList[].ResourceARN' --output table
