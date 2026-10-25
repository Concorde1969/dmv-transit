#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# First-boot script for the Lightsail instance (passed as user data by
# deploy/lightsail/create.ps1). Runs once, as root, via cloud-init.
#
# Clones the public repository and runs deploy/server-setup.sh (Docker, swap,
# automatic security updates, .env from the example). It does NOT start the
# site: API keys are added afterwards with deploy/lightsail/push-env.ps1.
#
# Progress: /var/log/cloud-init-output.log   Done when: cloud-init status --wait
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_URL="https://github.com/Concorde1969/dmv-transit.git"
OWNER="ubuntu"
PROJECT_DIR="/home/${OWNER}/dmvtransit"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq git

if [[ -d "$PROJECT_DIR/.git" ]]; then
  sudo -u "$OWNER" git -C "$PROJECT_DIR" pull --ff-only
else
  sudo -u "$OWNER" git clone "$REPO_URL" "$PROJECT_DIR"
fi

SUDO_USER="$OWNER" bash "$PROJECT_DIR/deploy/server-setup.sh"
echo "dmvtransit first-boot setup finished"
