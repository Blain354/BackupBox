# Pi File Transfer (Offline-friendly)

Application web (Flask) pour Raspberry Pi pour copier/couper des dossiers volumineux d'un stockage à un autre, avec UI mobile.

## Caractéristiques
- Détection des périphériques montés via `lsblk` (ou /media, /mnt en fallback)
- Navigation par arborescence (sélection de dossier source et dossier destination)
- Boutons **Copier**, **Couper**, **Éjecter**
- Barre de progression + pourcentage, vitesse (si `rsync`), ETA
- Fonctionne offline (aucune dépendance réseau)
- Démarrage automatique au boot via `systemd`

## Installation (mise à jour incluse)
1. Copiez le zip sur le Pi, puis décompressez:
   ```bash
   unzip pi-file-transfer.zip
   cd pi-file-transfer
   ```
2. Lancer le script d'installation (écrase la version précédente et redémarre le service):
   ```bash
   ./install.sh
   ```

Le service démarre automatiquement: `pi-file-transfer.service`.
Accès: `http://<IP-du-Pi>:8080`

## Éjection
Le bouton "Éjecter" tente `umount <mountpoint>` puis `sync`. Selon votre configuration, `sudo` peut être requis.
Si nécessaire, ajoutez pour `umount` l'autorisation sans mot de passe au groupe `pi` (optionnel).

## Notes
- `rsync` est utilisé par défaut pour des gros transferts fiables. Si `rsync` n'est pas disponible, un fallback Python plus lent est utilisé.
- Sur certaines images OS, les supports sont montés dans `/media/pi/<label>` ou `/run/media/pi/<label>`.

## Démarrage en AP (Access Point)
Cette app n'implique pas la config AP. Utilisez `raspi-config` ou des scripts (`hostapd`, `dnsmasq`). Une fois le Wi‑Fi AP actif, connectez votre téléphone et ouvrez l'IP du Pi sur le port 8080.

## Désinstallation
```bash
sudo systemctl disable --now pi-file-transfer.service
sudo rm -rf /opt/pi-file-transfer
sudo rm -f /etc/systemd/system/pi-file-transfer.service
sudo systemctl daemon-reload
```
