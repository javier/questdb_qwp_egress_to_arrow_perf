#!/usr/bin/env bash
# Shared config for the egress-bench AWS rig (SSH-driven; client + server in the
# same AZ / cluster placement group). Sourced by the laptop-side scripts.
# Requires on the laptop: aws CLI v2, an active credential/SSO session, ssh, rsync.
#
# Everything is overridable from the environment (EGB_* vars).

# Use whatever AWS credentials/profile are already in your environment. Set
# EGB_AWS_PROFILE only if you want to pin a named profile; otherwise the ambient
# AWS_PROFILE / default credential chain is used.
[ -n "${EGB_AWS_PROFILE:-}" ] && export AWS_PROFILE="$EGB_AWS_PROFILE"
export AWS_REGION="${EGB_AWS_REGION:-eu-west-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"

# Fail fast (with a hint) if you're not logged in. This does NOT log you in — bring
# your own session (e.g. `aws sso login` for your profile, or `aws configure`).
egb_ensure_auth() {
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        echo "ERROR: not authenticated to AWS. Log in first (e.g. 'aws sso login', or set" >&2
        echo "       AWS_PROFILE / credentials), then re-run. Region: ${AWS_REGION}." >&2
        exit 1
    fi
}

EGB_TAG_KEY="Project"
EGB_TAG_VAL="egress-bench"
EGB_PREFIX="egress-bench"

# Instance sizing. Defaults: 8 vCPU / 32 GB with ENA networking. Bump for the two
# things that bite this benchmark: RAM (connectorx + clickhouse/native buffer whole
# results; Timescale stores rows uncompressed) and network bandwidth (the egress path).
#   more RAM:        EGB_INSTANCE_TYPE=r7i.2xlarge   (64 GB)
#   more bandwidth:  EGB_INSTANCE_TYPE=c7gn.4xlarge  (up to 50 Gbps; set EGB_ARCH=arm64)
EGB_INSTANCE_TYPE="${EGB_INSTANCE_TYPE:-c8gn.2xlarge}"   # Graviton4, network-optimized (arm64)
# The two boxes want different things: the DB host needs RAM (the working set must stay
# page-cache resident or you measure the disk), the client mostly needs cores and NIC.
# Override either independently; both default to EGB_INSTANCE_TYPE.
EGB_SERVER_INSTANCE_TYPE="${EGB_SERVER_INSTANCE_TYPE:-$EGB_INSTANCE_TYPE}"
EGB_CLIENT_INSTANCE_TYPE="${EGB_CLIENT_INSTANCE_TYPE:-$EGB_INSTANCE_TYPE}"
EGB_ARCH="${EGB_ARCH:-arm64}"                     # x86_64 | arm64 (match the instance family)

# Root EBS. gp3, MAXED OUT so disk is never the bottleneck (esp. server-side reads:
# Timescale is uncompressed and reads from disk on a cold buffer pool). gp3 ceilings
# are 16000 IOPS / 1000 MiB/s; the default is only 3000 / 125.
EGB_ROOT_GB="${EGB_ROOT_GB:-120}"
EGB_EBS_IOPS="${EGB_EBS_IOPS:-16000}"
EGB_EBS_THROUGHPUT="${EGB_EBS_THROUGHPUT:-1000}"  # MiB/s

# Ubuntu 24.04 LTS via canonical's public SSM parameter (arch-templated).
EGB_UBUNTU_SSM_PARAM="/aws/service/canonical/ubuntu/server/24.04/stable/current/${EGB_ARCH}/hvm/ebs-gp3/ami-id"

# SSH key pair. Defaults to a rig-owned key that provision.sh creates (and saves the
# .pem beside these scripts). To reuse an EXISTING key instead, point both at it, e.g.
#   EGB_KEY_NAME=my-key EGB_KEY_FILE=~/.ssh/my-key.pem
# provision.sh reuses the key if it already exists in AWS, else creates EGB_KEY_NAME.
EGB_KEY_NAME="${EGB_KEY_NAME:-egress-bench-key}"
EGB_KEY_FILE="${EGB_KEY_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${EGB_KEY_NAME}.pem}"
# Marker written by provision.sh iff it CREATED the key pair. teardown.sh deletes the
# key only when this marker is present — so an auto-created key is cleaned up, and a
# reused/pre-existing key is never touched.
EGB_KEY_CREATED_MARKER="${EGB_KEY_FILE}.rig-created"
EGB_SSH_USER="ubuntu"
EGB_REMOTE_DIR="/opt/egress_perf"

# Repo root on the laptop (the directory rsync'd to each box).
EGB_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

egb_account_id() { aws sts get-caller-identity --query Account --output text; }
egb_tagspec()    { echo "ResourceType=$1,Tags=[{Key=${EGB_TAG_KEY},Value=${EGB_TAG_VAL}}]"; }

# $1 = role tag value: server | client
egb_instance_id() {
    aws ec2 describe-instances \
        --filters "Name=tag:${EGB_TAG_KEY},Values=${EGB_TAG_VAL}" \
                  "Name=tag:Role,Values=$1" \
                  "Name=instance-state-name,Values=pending,running,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text
}
egb_public_ip()  { aws ec2 describe-instances --instance-ids "$1" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text; }
egb_private_ip() { aws ec2 describe-instances --instance-ids "$1" --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text; }
