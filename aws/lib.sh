#!/usr/bin/env bash
# SSH/rsync helpers over the provisioned boxes. Sourced after env.sh.

EGB_SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null
              -o LogLevel=ERROR -o ConnectTimeout=10 -i "$EGB_KEY_FILE")

egb_ip_of() {  # role -> public IP
    egb_public_ip "$(egb_instance_id "$1")"
}

egb_ssh() {  # role, command-string...
    local role="$1"; shift
    ssh "${EGB_SSH_OPTS[@]}" "${EGB_SSH_USER}@$(egb_ip_of "$role")" "$@"
}

# Push the repo to a box (minus git/results/venv cruft). The vendored template CSV
# IS included so the box can generate data.
egb_push_repo() {  # role
    local role="$1"
    # RESULTS.md is excluded so a local run log never contaminates the box — each rig
    # run generates a fresh RESULTS.md on the client, which run.sh scp's to results/aws/.
    rsync -az --delete -e "ssh ${EGB_SSH_OPTS[*]}" \
        --exclude '.git' --exclude 'results' --exclude 'RESULTS.md' --exclude '.venv' \
        --exclude '.pyqdb_build' --exclude '__pycache__' --exclude 'aws/*.pem' \
        "${EGB_REPO_DIR}/" "${EGB_SSH_USER}@$(egb_ip_of "$role"):${EGB_REMOTE_DIR}/"
}

egb_scp_from() {  # role, remote-path, local-path
    scp "${EGB_SSH_OPTS[@]}" "${EGB_SSH_USER}@$(egb_ip_of "$1"):$2" "$3"
}

# Wait until sshd answers on a box.
egb_wait_ssh() {  # role
    local role="$1"
    for _ in $(seq 1 60); do
        if egb_ssh "$role" true 2>/dev/null; then return 0; fi
        sleep 5
    done
    echo "ERROR: ssh to '$role' never came up" >&2
    return 1
}
