#!/usr/bin/env bash
# Install or update BackupBox on Raspberry Pi OS Lite (64-bit). Idempotent.
# Usage, from a checkout of the repository:  sudo deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Lancer avec sudo : sudo $0" >&2
  exit 1
fi

SRC=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=/opt/backupbox
APP="$PREFIX/app"
VENV="$PREFIX/venv"
USER_NAME=backupbox

step() { printf '\n[*] %s\n' "$*"; }

step "Paquets système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv udisks2 exfatprogs ntfs-3g dosfstools util-linux rsync curl
apt-get install -y --no-install-recommends polkitd || apt-get install -y --no-install-recommends policykit-1

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
if [[ "$PY_OK" != "1" ]]; then
  echo "[!] Python 3.11+ requis (Raspberry Pi OS Bookworm ou plus récent)." >&2
  exit 1
fi

step "Utilisateur de service « $USER_NAME »"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/backupbox --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
if getent group video >/dev/null; then usermod -aG video "$USER_NAME"; fi

step "Code dans $APP (le venv $VENV est conservé)"
install -d -m 0755 "$PREFIX" "$APP"
rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'venv/' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude 'tests/' --exclude 'sim/' \
  "$SRC"/ "$APP"/
chown -R root:root "$APP"

step "Environnement Python"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade --quiet pip
"$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"

step "Règle polkit (montage udisks2 et arrêt sans root)"
install -D -m 0644 "$APP/deploy/polkit/50-backupbox.rules" /etc/polkit-1/rules.d/50-backupbox.rules
systemctl try-restart polkit.service 2>/dev/null || true

step "Règle udev de la DEL verte"
install -D -m 0644 "$APP/deploy/udev/99-backupbox-led.rules" /etc/udev/rules.d/99-backupbox-led.rules
udevadm control --reload
udevadm trigger --subsystem-match=leds --action=add || true

step "Service systemd"
install -D -m 0644 "$APP/deploy/systemd/backupbox.service" /etc/systemd/system/backupbox.service
systemctl daemon-reload
systemctl enable udisks2.service >/dev/null 2>&1 || true
systemctl enable backupbox.service
systemctl restart backupbox.service

step "Vérification"
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1/api/health >/dev/null; then
    curl -s http://127.0.0.1/api/health; echo
    echo "[✓] BackupBox répond sur le port 80."
    exit 0
  fi
  sleep 1
done
echo "[!] Le service ne répond pas. Journal :" >&2
journalctl -u backupbox -n 50 --no-pager >&2
exit 1
