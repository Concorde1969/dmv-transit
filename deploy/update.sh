#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy the latest code from GitHub. Run on the server from anywhere:
#
#     bash ~/dmvtransit/deploy/update.sh
#
# Data, certificates and .env are untouched. The site is briefly unavailable
# (a few seconds) while the new version starts.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> Fetching the latest code"
git pull --ff-only

echo "==> Rebuilding and restarting"
docker compose up -d --build

echo "==> Cleaning up old images"
docker image prune -f >/dev/null

echo "==> Status"
docker compose ps
echo
echo "Health check: docker compose exec app transit-mcp doctor"
