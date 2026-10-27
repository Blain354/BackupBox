#!/usr/bin/env bash
# Turn wlan0 into the "BackupBox" Wi-Fi access point with NetworkManager.
# Usage:  sudo deploy/setup-hotspot.sh [--no-portal] <mot-de-passe-wifi> [SSID] [interface]
# The phone then opens http://10.42.0.1 (or http://backupbox.lan).
# By default every DNS name resolves to the Pi on this network (captive portal):
# when the phone joins « BackupBox », Android/iOS show « Se connecter au réseau »,
# which opens BackupBox. --no-portal only answers backupbox.lan.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Lancer avec sudo." >&2
  exit 1
fi
PORTAL=1
if [[ "${1:-}" == "--no-portal" ]]; then
  PORTAL=0
  shift
fi
PSK=${1:-}
SSID=${2:-BackupBox}
IFACE=${3:-wlan0}
CON=BackupBox-AP

if [[ ${#PSK} -lt 8 ]]; then
  echo "Usage : sudo $0 <mot-de-passe d'au moins 8 caractères> [SSID] [interface]" >&2
  exit 1
fi
if ! command -v nmcli >/dev/null; then
  echo "[!] NetworkManager absent : Raspberry Pi OS Bookworm ou plus récent requis." >&2
  exit 1
fi
if rfkill list wifi 2>/dev/null | grep -q "Soft blocked: yes"; then
  echo "[!] Wi-Fi bloqué : définir le pays Wi-Fi (sudo raspi-config nonint do_wifi_country CA)." >&2
  exit 1
fi

nmcli connection delete "$CON" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$IFACE" con-name "$CON" autoconnect yes ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  wifi-sec.psk "$PSK" connection.autoconnect-priority 100

# Friendly name inside the hotspot (NetworkManager's dnsmasq reads this folder).
install -d /etc/NetworkManager/dnsmasq-shared.d
{
  echo "address=/backupbox.lan/10.42.0.1"
  if [[ $PORTAL -eq 1 ]]; then
    echo "# captive portal: every name -> the Pi (the hotspot has no internet anyway)"
    echo "address=/#/10.42.0.1"
  fi
} > /etc/NetworkManager/dnsmasq-shared.d/backupbox.conf

nmcli connection up "$CON"
echo "[✓] Point d'accès « $SSID » actif. Interface : http://10.42.0.1"
