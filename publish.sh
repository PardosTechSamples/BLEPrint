#!/usr/bin/env bash
# Publish BLEPrint to GitHub (PardosTechSamples/BLEPrint, public) and push main.
set -euo pipefail
cd "$(dirname "$0")"
gh repo create PardosTechSamples/BLEPrint --public --source=. --remote=origin --push --description "Open-source Bluetooth LE label printer for Nelko P21 and Supvan E10 (Mac & Windows)."
echo "Published: https://github.com/PardosTechSamples/BLEPrint"
