#!/usr/bin/env bash
# Provision the 2-box egress-bench rig (SSH-driven, same AZ).
# Creates: key pair (saved locally), security group (SSH from your laptop IP +
# all intra-group traffic), cluster placement group, 2 tagged instances with a
# MAXED gp3 root volume. Reuses the default VPC. Aborts if instances already exist.
set -euo pipefail
cd "$(dirname "$0")"
. ./env.sh
egb_ensure_auth

SG_NAME="${EGB_PREFIX}-sg"
PG_NAME="${EGB_PREFIX}-pg"

if [ -n "$(egb_instance_id server)$(egb_instance_id client)" ]; then
    echo "ERROR: tagged instances already exist — run ./teardown.sh first." >&2
    exit 1
fi

echo "== default VPC"
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)
[ "$VPC_ID" != "None" ] || { echo "ERROR: no default VPC in $AWS_REGION" >&2; exit 1; }

echo "== find AZs offering BOTH box types (both boxes go in ONE AZ for low RTT)"
echo "   server=$EGB_SERVER_INSTANCE_TYPE  client=$EGB_CLIENT_INSTANCE_TYPE"
az_list() {  # AZs offering instance type $1
    aws ec2 describe-instance-type-offerings --location-type availability-zone \
        --filters "Name=instance-type,Values=$1" \
        --query 'InstanceTypeOfferings[].Location' --output text | tr '\t' '\n' | sort -u
}
if [ -n "${EGB_AZ:-}" ]; then
    CANDIDATE_AZS="$EGB_AZ"                       # pin an AZ explicitly if you want one
else
    CANDIDATE_AZS=$(comm -12 <(az_list "$EGB_SERVER_INSTANCE_TYPE") \
                             <(az_list "$EGB_CLIENT_INSTANCE_TYPE"))
fi
[ -n "$CANDIDATE_AZS" ] || { echo "ERROR: no AZ offers both instance types" >&2; exit 1; }
echo "   candidates: $(echo $CANDIDATE_AZS)"
SUBNET_ID=""; AZ=""

echo "== key pair '$EGB_KEY_NAME'"
if aws ec2 describe-key-pairs --key-names "$EGB_KEY_NAME" >/dev/null 2>&1; then
    echo "   reusing existing AWS key pair '$EGB_KEY_NAME'"
    [ -f "$EGB_KEY_FILE" ] || {
        echo "ERROR: AWS key '$EGB_KEY_NAME' exists but local private key $EGB_KEY_FILE is missing." >&2
        echo "       Point EGB_KEY_FILE at your .pem, or pick a different EGB_KEY_NAME." >&2
        exit 1; }
else
    echo "   creating new key pair -> $EGB_KEY_FILE"
    aws ec2 create-key-pair --key-name "$EGB_KEY_NAME" \
        --tag-specifications "$(egb_tagspec key-pair)" \
        --query KeyMaterial --output text > "$EGB_KEY_FILE"
    chmod 600 "$EGB_KEY_FILE"
    : > "$EGB_KEY_CREATED_MARKER"   # tell teardown.sh this key is ours to delete
fi

echo "== security group (SSH from your laptop + all traffic intra-group)"
# Reuse rather than abort: a provisioning run that dies partway (capacity, a network
# blip) leaves this behind, and refusing to start again because of our own debris is
# a bad failure mode. Rules are re-applied every time, which also picks up a changed
# laptop IP; duplicates are expected and ignored.
SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
    echo "   reusing existing $SG_NAME ($SG_ID)"
else
    SG_ID=$(aws ec2 create-security-group --group-name "$SG_NAME" \
        --description "egress-bench: SSH from laptop, all traffic intra-group" \
        --vpc-id "$VPC_ID" --tag-specifications "$(egb_tagspec security-group)" \
        --query GroupId --output text)
fi
MY_IP=$(curl -fsS https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "${MY_IP}/32" >/dev/null 2>&1 || true
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol -1 --source-group "$SG_ID" >/dev/null 2>&1 || true
echo "   ssh allowed from ${MY_IP}/32"

echo "== cluster placement group (both boxes co-located for lowest RTT)"
aws ec2 create-placement-group --group-name "$PG_NAME" --strategy cluster \
    --tag-specifications "$(egb_tagspec placement-group)" >/dev/null 2>&1 || true

echo "== AMI (Ubuntu 24.04 $EGB_ARCH)"
AMI=$(aws ssm get-parameter --name "$EGB_UBUNTU_SSM_PARAM" --query Parameter.Value --output text)
echo "   $AMI"

launch() {  # $1 = role
    local itype="$EGB_SERVER_INSTANCE_TYPE"
    [ "$1" = "client" ] && itype="$EGB_CLIENT_INSTANCE_TYPE"
    aws ec2 run-instances \
        --image-id "$AMI" --instance-type "$itype" \
        --key-name "$EGB_KEY_NAME" \
        --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
        --associate-public-ip-address \
        --placement "GroupName=$PG_NAME" \
        --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
        --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${EGB_ROOT_GB},VolumeType=gp3,Iops=${EGB_EBS_IOPS},Throughput=${EGB_EBS_THROUGHPUT},DeleteOnTermination=true}" \
        --tag-specifications \
            "ResourceType=instance,Tags=[{Key=${EGB_TAG_KEY},Value=${EGB_TAG_VAL}},{Key=Role,Value=$1},{Key=Name,Value=${EGB_PREFIX}-$1}]" \
            "ResourceType=volume,Tags=[{Key=${EGB_TAG_KEY},Value=${EGB_TAG_VAL}}]" \
        --query 'Instances[0].InstanceId' --output text
}

echo "== launch server + client (gp3 ${EGB_ROOT_GB}GB @ ${EGB_EBS_IOPS} IOPS / ${EGB_EBS_THROUGHPUT} MiB/s)"
# An AZ can OFFER a type and still have no capacity right now, and the failure usually
# lands on the second instance. Launching both without cleaning up in between leaks a
# running instance that bills until someone notices, so on failure we tear the partial
# rig down and try the next AZ.
SERVER_ID=""; CLIENT_ID=""
for az in $CANDIDATE_AZS; do
    sn=$(aws ec2 describe-subnets \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=availability-zone,Values=$az" \
        --query 'Subnets[0].SubnetId' --output text)
    [ "$sn" != "None" ] && [ -n "$sn" ] || continue
    SUBNET_ID="$sn"; AZ="$az"
    echo "   trying $AZ ..."
    if ! SERVER_ID=$(launch server 2>/dev/null); then
        echo "   no capacity for $EGB_SERVER_INSTANCE_TYPE in $AZ"
        SERVER_ID=""; continue
    fi
    if ! CLIENT_ID=$(launch client 2>/dev/null); then
        echo "   no capacity for $EGB_CLIENT_INSTANCE_TYPE in $AZ, releasing the server"
        aws ec2 terminate-instances --instance-ids "$SERVER_ID" >/dev/null 2>&1
        SERVER_ID=""; CLIENT_ID=""; continue
    fi
    break
done
[ -n "$SERVER_ID" ] && [ -n "$CLIENT_ID" ] || {
    echo "ERROR: could not launch both instance types in any AZ with a default subnet." >&2
    echo "       Try different types via EGB_SERVER_INSTANCE_TYPE / EGB_CLIENT_INSTANCE_TYPE." >&2
    exit 1; }
echo "   server=$SERVER_ID client=$CLIENT_ID in $AZ"

echo "== wait: running"
aws ec2 wait instance-running --instance-ids "$SERVER_ID" "$CLIENT_ID"

echo
echo "== READY =="
echo "server: $SERVER_ID  public=$(egb_public_ip "$SERVER_ID")  private=$(egb_private_ip "$SERVER_ID")"
echo "client: $CLIENT_ID  public=$(egb_public_ip "$CLIENT_ID")  private=$(egb_private_ip "$CLIENT_ID")"
echo "az: $AZ   pg: $PG_NAME   key: $EGB_KEY_FILE"
echo "Next: ./bootstrap.sh    (installs Docker on both, builds the client image)"
