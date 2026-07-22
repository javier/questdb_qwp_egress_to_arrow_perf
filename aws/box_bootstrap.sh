#!/usr/bin/env bash
# Runs ON a box (server or client), shipped with the repo and invoked over SSH.
# Installs Docker Engine + the compose plugin (idempotent). Building the client
# image and pulling the DB images happen in later, separate SSH sessions (so the
# freshly-added 'docker' group membership is active).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get update -q
    sudo apt-get install -qy ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -q
    sudo apt-get install -qy docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker "$USER"
fi
sudo systemctl enable --now docker
echo "== docker installed: $(sudo docker --version)"
