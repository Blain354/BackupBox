#!/usr/bin/env bash
set -euo pipefail

# Usage: ./setup.sh [ZIP_URL]
# Downloads the project archive from the internet and runs the install script.
# Example: ./setup.sh https://example.com/pi-file-transfer.zip

ZIP_URL="${1:-}" 
if [[ -z "$ZIP_URL" ]]; then
  echo "Usage: $0 <zip-url>" >&2
  exit 1
fi

TMP_DIR=$(mktemp -d)
ZIP_FILE="$TMP_DIR/project.zip"

echo "[*] Downloading project archive..."
curl -L "$ZIP_URL" -o "$ZIP_FILE"

echo "[*] Extracting archive..."
unzip -q "$ZIP_FILE" -d "$TMP_DIR"

# Assume archive contains a single directory
PROJECT_DIR=$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [[ -z "$PROJECT_DIR" ]]; then
  echo "[!] Could not find project directory in archive" >&2
  exit 1
fi

cd "$PROJECT_DIR"

if [[ ! -x ./install.sh ]]; then
  echo "[!] install.sh not found in project" >&2
  exit 1
fi

echo "[*] Running install script..."
./install.sh

