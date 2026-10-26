#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Download the regional basemap the site's map uses (run on the server):
#
#     bash ~/dmvtransit/deploy/map/fetch-basemap.sh
#
# Cuts the DC / Maryland / Virginia region out of Protomaps' free daily world build
# (OpenStreetMap data) into ~/dmvtransit/map/dmv.pmtiles, which docker-compose mounts
# into the app. Uses the pinned pmtiles tool from github.com/protomaps/go-pmtiles.
# Safe to re-run to refresh the map (a few times a year is plenty).
# ---------------------------------------------------------------------------
set -euo pipefail

PMTILES_VERSION="1.31.2"
BUILD="${1:-20261026}"                         # Protomaps build date (YYYYMMDD)
BBOX="-79.3,37.4,-75.3,40.1"                    # west,south,east,north
MAXZOOM=15
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/map"
TOOL_DIR="$DIR/.tool"

# docker compose creates the mounted folder as root if it starts before this script.
if [[ -d "$DIR" && ! -w "$DIR" ]]; then sudo chown "$(id -u):$(id -g)" "$DIR"; fi
mkdir -p "$DIR" "$TOOL_DIR"
if [[ ! -x "$TOOL_DIR/pmtiles" ]]; then
  arch="$(uname -m)"; case "$arch" in x86_64) arch=x86_64 ;; aarch64|arm64) arch=arm64 ;; esac
  url="https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_${arch}.tar.gz"
  echo "==> Downloading the pmtiles tool ($url)"
  curl -fsSL "$url" | tar -xz -C "$TOOL_DIR" pmtiles
fi

echo "==> Extracting the region from build $BUILD (this downloads only the region's tiles)"
"$TOOL_DIR/pmtiles" extract "https://build.protomaps.com/${BUILD}.pmtiles" "$DIR/dmv.pmtiles.new" \
  --bbox="$BBOX" --maxzoom="$MAXZOOM"
mv "$DIR/dmv.pmtiles.new" "$DIR/dmv.pmtiles"
ls -lh "$DIR/dmv.pmtiles"
echo "Done. Reload the site to see the map."
