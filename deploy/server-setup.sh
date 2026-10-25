#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-time setup for a fresh Ubuntu 24.04 server (e.g. AWS Lightsail).
# Run from inside the cloned project folder:
#
#     sudo bash deploy/server-setup.sh
#
# Safe to run again: every step checks whether it's already done.
# Installs Docker, adds swap, turns on automatic security updates, and creates
# .env for you to fill in. It does not start the site — see deploy/README.md.
# ---------------------------------------------------------------------------
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash deploy/server-setup.sh" >&2
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="${SUDO_USER:-ubuntu}"
export DEBIAN_FRONTEND=noninteractive

step() { printf '\n==> %s\n' "$*"; }

step "Updating the operating system"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl gnupg git unattended-upgrades

step "Turning on automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

step "Installing Docker"
if docker compose version >/dev/null 2>&1; then
  echo "Docker is already installed."
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$OWNER"

step "Adding 2 GB of swap (protects against out-of-memory during data loads)"
if swapon --show | grep -q '/swapfile'; then
  echo "Swap already present."
else
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

step "Preparing folders and settings"
install -d -o "$OWNER" -g "$OWNER" "$PROJECT_DIR/feeds/cue"
if [[ -f "$PROJECT_DIR/.env" ]]; then
  echo ".env already exists — leaving it alone."
else
  cp "$PROJECT_DIR/deploy/.env.production.example" "$PROJECT_DIR/.env"
  chown "$OWNER:$OWNER" "$PROJECT_DIR/.env"
  chmod 600 "$PROJECT_DIR/.env"
  echo "Created $PROJECT_DIR/.env"
fi

# Next-step instructions only for a person at a terminal (not first-boot user data).
[[ -t 1 ]] || exit 0

cat <<EOF

------------------------------------------------------------------------------
 Setup complete. Next:

   1. Log out and back in (so your user can run docker without sudo).
   2. Fill in your settings:       nano $PROJECT_DIR/.env
   3. Start the site:              cd $PROJECT_DIR && docker compose up -d --build
   4. Watch the first data load:   docker compose logs -f scheduler

 Full guide: deploy/README.md
------------------------------------------------------------------------------
EOF
