\
#!/usr/bin/env bash
set -euo pipefail

APP_NAME="pi-file-transfer"
INSTALL_DIR="/opt/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"

echo "[*] Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip rsync lsblk

echo "[*] Deploying to ${INSTALL_DIR}..."
sudo mkdir -p "${INSTALL_DIR}"
sudo cp -r ./* "${INSTALL_DIR}/"

cd "${INSTALL_DIR}"
if [ ! -d venv ]; then
  sudo python3 -m venv venv
fi
sudo ./venv/bin/pip install --upgrade pip
sudo ./venv/bin/pip install -r requirements.txt

echo "[*] Setting permissions..."
sudo chown -R pi:pi "${INSTALL_DIR}" || true

echo "[*] Installing systemd service..."
sudo cp "${INSTALL_DIR}/systemd/pi-file-transfer.service" "${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl enable "${APP_NAME}.service"
sudo systemctl restart "${APP_NAME}.service"

echo "[*] Done. The app should now be running on port 8080."
echo "    Access: http://<pi-ip>:8080"
