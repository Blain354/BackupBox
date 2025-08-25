#!/usr/bin/env bash
set -euo pipefail

# Usage: ./update.sh <archive.zip>
# Overwrites the installed project with the content of the provided archive without requiring internet.

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <archive.zip>" >&2
  exit 1
fi

ARCHIVE="$1"
APP_NAME="pi-file-transfer"
INSTALL_DIR="/opt/${APP_NAME}"

if [[ ! -f "$ARCHIVE" ]]; then
  echo "[!] Archive $ARCHIVE not found" >&2
  exit 1
fi

TMP_DIR=$(mktemp -d)

echo "[*] Extracting $ARCHIVE..."
unzip -q "$ARCHIVE" -d "$TMP_DIR"

PROJECT_DIR="$TMP_DIR/$APP_NAME"
if [[ ! -d "$PROJECT_DIR" ]]; then
  # Fallback: take first directory
  PROJECT_DIR=$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "[!] Project directory not found in archive" >&2
  exit 1
fi

echo "[*] Updating files in $INSTALL_DIR..."
sudo rsync -a --delete "$PROJECT_DIR/" "$INSTALL_DIR/"

if command -v systemctl >/dev/null 2>&1; then
  echo "[*] Restarting service..."
  sudo systemctl restart "${APP_NAME}.service" || true
fi

echo "[*] Update complete."

