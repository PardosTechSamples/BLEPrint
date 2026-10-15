#!/usr/bin/env bash
# Build BLEPrint from source into a standalone app.
#   macOS  -> dist/BLEPrint.app
#   Linux/Windows -> dist/BLEPrint/ (folder with the BLEPrint executable)
#
# Usage:  ./build.sh
# Override the interpreter with:  PYTHON=python3.12 ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".buildvenv"

if [ ! -d "$VENV" ]; then
  echo "==> Creating build venv ($VENV)"
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate" 2>/dev/null || source "$VENV/Scripts/activate"

echo "==> Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet PyQt6 bleak pillow pyinstaller

echo "==> Building with PyInstaller"
pyinstaller --noconfirm BLEPrint.spec

echo
if [ -d "dist/BLEPrint.app" ]; then
  echo "✅ Built: dist/BLEPrint.app"
  echo "   Run it, or: open dist/BLEPrint.app"
else
  echo "✅ Built: dist/BLEPrint/  (run dist/BLEPrint/BLEPrint)"
fi
