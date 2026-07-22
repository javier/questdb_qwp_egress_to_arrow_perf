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

echo "== pick an AZ offering $EGB_INSTANCE_TYPE (both boxes go in ONE AZ for low RTT)"
OFFERED=$(aws ec2 describe-instance-type-offerings --location-type availability-zone \
    --filters "Name=instance-type,Values=${EGB_INSTANCE_TYPE}" \
    --query 'InstanceTypeOfferings[].Location' --output text)
SUBNET_ID=""; AZ=""
for az in $OFFERED; do
    sn=$(aws ec2 describe-subnets \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=availability-zone,Values=$az" \
        --query 'Subnets[0].SubnetId' --output text)
    if [ "$sn" != "None" ] && [ -n "$sn" ]; then SUBNET_ID=$sn; AZ=$az; break; fi
done
[ -n "$SUBNET_ID" ] || { echo "ERROR: no default subnet in an AZ offering ${EGB_INSTANCE_TYPE}" >&2; exit 1; }
echo "   AZ=$AZ subnet=$SUBNET_ID"

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
MY_IP=$(curl -fsS https://checkip.amazonaws.com)
SG_ID=$(aws ec2 create-security-group --group-name "$SG_NAME" \
    --description "egress-bench: SSH from laptop, all traffic intra-group" \
    --vpc-id "$VPC_ID" --tag-specifications "$(egb_tagspec security-group)" \
    --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "${MY_IP}/32" >/dev/null
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol -1 --source-group "$SG_ID" >/dev/null

echo "== cluster placement group (both boxes co-located for lowest RTT)"
aws ec2 create-placement-group --group-name "$PG_NAME" --strategy cluster \
    --tag-specifications "$(egb_tagspec placement-group)" >/dev/null 2>&1 || true

echo "== AMI (Ubuntu 24.04 $EGB_ARCH)"
AMI=$(aws ssm get-parameter --name "$EGB_UBUNTU_SSM_PARAM" --query Parameter.Value --output text)
echo "   $AMI"

launch() {  # $1 = role
    aws ec2 run-instances \
        --image-id "$AMI" --instance-type "$EGB_INSTANCE_TYPE" \
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
SERVER_ID=$(launch server)
CLIENT_ID=$(launch client)
echo "   server=$SERVER_ID client=$CLIENT_ID"

echo "== wait: running"
aws ec2 wait instance-running --instance-ids "$SERVER_ID" "$CLIENT_ID"

echo
echo "== READY =="
echo "server: $SERVER_ID  public=$(egb_public_ip "$SERVER_ID")  private=$(egb_private_ip "$SERVER_ID")"
echo "client: $CLIENT_ID  public=$(egb_public_ip "$CLIENT_ID")  private=$(egb_private_ip "$CLIENT_ID")"
echo "az: $AZ   pg: $PG_NAME   key: $EGB_KEY_FILE"
echo "Next: ./bootstrap.sh    (installs Docker on both, builds the client image)"
