#!/usr/bin/env bash
# Build dist/arc-lab-series.zip for uploading to claude.ai (Settings > Capabilities > Skills)
# or for sharing as a download.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dist
rm -f dist/arc-lab-series.zip
(cd skills && zip -qr ../dist/arc-lab-series.zip arc-lab-series -x '*/__pycache__/*' '*.mp4' '*.srt' '*_cover.png')
echo "dist/arc-lab-series.zip"
