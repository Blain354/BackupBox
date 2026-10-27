# BackupBox : configuration du Raspberry Pi

Ce guide s'adresse à l'agent (ou à la personne) qui prépare le Raspberry Pi. Il dit ce dont l'application a besoin au niveau du système, pourquoi, comment l'installer et comment vérifier que c'est fait. Les notes de conception sont dans [DESIGN.md](DESIGN.md).

> **État de validation.** L'application et ses 83 tests ont été validés en simulation, sur Linux x86-64. **Rien n'a encore tourné sur un vrai Pi.** La section 9 est la recette à exécuter sur le matériel. Tout écart y est un bogue à signaler, pas une étape à contourner.

---

## 0. Résumé pour un agent pressé

```bash
# Sur Raspberry Pi OS Lite 64 bits, avec Internet (Ethernet), en tant qu'administrateur :
sudo raspi-config nonint do_hostname backupbox
sudo raspi-config nonint do_wifi_country CA
sudo timedatectl set-timezone America/Toronto
git clone https://github.com/Blain354/BackupBox.git ~/BackupBox
cd ~/BackupBox
sudo deploy/install.sh                      # paquets, utilisateur, polkit, udev, service
sudo deploy/setup-hotspot.sh '<mot-de-passe-wifi>'   # point d'accès « BackupBox »
sudo reboot
# puis exécuter la recette de la section 9
```

---

## 1. Ce dont l'application a besoin

| Besoin | Pourquoi | Comment c'est fourni |
|---|---|---|
| Python ≥ 3.11 et un venv | Code de l'application (FastAPI, uvicorn, xxhash) | `python3`, `python3-venv` ; venv dans `/opt/backupbox/venv` |
| Lister les disques | Détecter cartes et SSD à chaud (sondage `lsblk` toutes les 2 s) | `util-linux` (installé par défaut) |
| Monter / démonter **sans root** | Cartes montées **en lecture seule**, SSD en lecture-écriture | `udisks2` + règle polkit pour l'utilisateur `backupbox` |
| Mettre le SSD hors tension | Éjection propre (vidage du cache du SSD) | `udisksctl power-off`, même règle polkit |
| Lire exFAT, FAT32, NTFS | Cartes SDXC (exFAT), SDHC (FAT32), SSD (exFAT ou NTFS) | noyau (exfat, vfat, ntfs3) + `exfatprogs`, `dosfstools`, `ntfs-3g` |
| Écouter sur le port 80 | Le téléphone tape seulement `http://10.42.0.1` | `AmbientCapabilities=CAP_NET_BIND_SERVICE` dans l'unité systemd |
| Éteindre / redémarrer depuis l'interface | Arrêt propre sur le terrain (protège la carte microSD système) | règle polkit `org.freedesktop.login1.power-off` / `reboot` |
| Piloter la DEL verte (ACT) | Témoin sans téléphone : cœur = copie, fixe = fini, clignote vite = erreur | règle udev qui donne au groupe `backupbox` l'écriture sur `trigger` et `brightness` |
| Lire l'état d'alimentation | Avertir en cas de sous-tension (cause n° 1 des déconnexions USB) | `/sys/devices/platform/soc/soc:firmware/get_throttled` (repli : `vcgencmd`, groupe `video`) |
| Un réseau Wi-Fi propre au Pi | Utilisation en voyage, sans Internet | NetworkManager en mode point d'accès (`deploy/setup-hotspot.sh`) |
| Stocker réglages et historique | Survivre aux redémarrages | `/var/lib/backupbox` (`StateDirectory=` systemd) |

Ce que l'application **ne** demande **pas** : root, sudo, une entrée dans `/etc/fstab`, un montage automatique par le système, l'accès à Internet après l'installation.

---

## 2. Matériel

| Élément | Exigence | Remarque |
|---|---|---|
| Carte | **Raspberry Pi 4** (2, 4 ou 8 Go) | 2 Go suffisent : l'application utilise < 150 Mo de RAM |
| Alimentation | **Officielle 5,1 V / 3 A USB-C** | Le Pi 4 limite l'ensemble des ports USB à 1,2 A |
| Carte microSD système | 16–32 Go, classe A1/A2, de marque | Seulement le système : les rushes ne passent jamais dessus |
| Lecteur de cartes | USB 3, sur un **port bleu** (USB 3) | UHS-II conseillé si les cartes le sont. Un seul lecteur suffit |
| SSD de destination | USB 3, **exFAT** | Sur l'autre port bleu. En ext4, la racine appartient à root : faire une fois `sudo chown backupbox: /media/backupbox/<ÉTIQUETTE>`, sinon l'initialisation échoue (« Permission denied ») |
| Boîtier | Ventilé ou dissipateur | Une copie de plusieurs centaines de Go chauffe le SoC |

**Batterie externe :** le Pi 4 ne négocie pas l'USB Power Delivery. Il faut une batterie capable de fournir 3 A sous 5 V sans négociation, sinon il y aura des sous-tensions. L'application les signale, mais ne peut pas les corriger. Si le SSD décroche, utiliser un **hub USB 3 alimenté** pour le SSD.

**Horloge :** le Pi 4 n'a pas d'horloge temps réel à pile. Sans réseau, l'heure du système repart de la dernière heure connue (`fake-hwclock`). Ce n'est pas bloquant : BackupBox conserve la date de modification d'origine des fichiers, et Footage Organizer lit les métadonnées des clips. Seules les dates de l'historique peuvent être fausses.

---

## 3. Système d'exploitation

- **Raspberry Pi OS Lite 64 bits** (Bookworm ou plus récent). **Lite est obligatoire** : le bureau installe un automonteur (pcmanfm/udiskie) qui monterait les cartes **en écriture**, au nom d'un autre utilisateur, avant BackupBox.
- Avec Raspberry Pi Imager :
  - nom d'hôte `backupbox` ;
  - un compte administrateur, au nom libre (l'utilisateur `pi` n'existe plus par défaut et l'application n'en dépend pas) ;
  - SSH activé ;
  - pays Wi-Fi **CA** (sinon le Wi-Fi reste bloqué par rfkill) ;
  - fuseau `America/Toronto`.
- L'installation se fait **par Ethernet** : le Wi-Fi deviendra le point d'accès.

À **ne pas** faire :
- Installer un environnement de bureau, `udiskie`, `usbmount` ou tout autre automonteur.
- Ajouter le SSD ou les cartes dans `/etc/fstab`. La détection du rôle (carte ou SSD) passe par udisks2, et le démarrage bloquerait si le disque est absent.
- Lancer le service en root, ou remplacer la règle polkit par un `sudoers` : le moindre privilège est voulu.
- Activer un système de fichiers racine en lecture seule (overlay de `raspi-config`) pour la v1 : les réglages et l'historique ne survivraient pas au redémarrage.

---

## 4. Installation de l'application

```bash
git clone https://github.com/Blain354/BackupBox.git ~/BackupBox
cd ~/BackupBox
sudo deploy/install.sh
```

`deploy/install.sh` est idempotent ; il sert aussi aux mises à jour. Il :

1. installe `python3 python3-venv udisks2 polkitd exfatprogs ntfs-3g dosfstools util-linux rsync curl` ;
2. crée l'utilisateur système `backupbox` (sans connexion, ajouté au groupe `video`) ;
3. copie le code dans `/opt/backupbox/app`, possédé par root et en lecture seule pour le service ;
4. crée le venv `/opt/backupbox/venv`, **séparé du code**, ce qui corrige le défaut de l'ancienne version où chaque mise à jour effaçait le venv ;
5. installe `deploy/polkit/50-backupbox.rules` dans `/etc/polkit-1/rules.d/` ;
6. installe `deploy/udev/99-backupbox-led.rules` et l'applique ;
7. installe et démarre `backupbox.service`, puis vérifie `http://127.0.0.1/api/health`.

### 4.1 Unité systemd (`deploy/systemd/backupbox.service`)

Les points importants :

- `User=backupbox` et `NoNewPrivileges=yes` ;
- `AmbientCapabilities=CAP_NET_BIND_SERVICE`, seule capacité accordée, pour le port 80 ;
- `StateDirectory=backupbox`, d'où `/var/lib/backupbox` en 0750 ;
- `Wants=`/`After=udisks2.service`.

**Ne pas** ajouter `ProtectSystem=`, `PrivateTmp=`, `PrivateMounts=` ni `ProtectHome=`. Ces options placent le service dans son propre espace de montage. Selon la propagation, les montages faits ensuite par udisks2 sous `/media/backupbox/` peuvent alors ne pas être visibles.

### 4.2 Règle polkit (`deploy/polkit/50-backupbox.rules`)

Elle autorise **uniquement l'utilisateur `backupbox`**, et **uniquement** pour :
- les montages, démontages, éjections et mises hors tension udisks2. Les variantes `*-other-seat` sont nécessaires, car un service n'a pas de session ;
- `login1.power-off` et `login1.reboot`.

Polkit ≥ 0.106 (règles JavaScript) est requis. C'est le cas de Bookworm et des versions suivantes (paquet `polkitd`).

### 4.3 DEL verte (`deploy/udev/99-backupbox-led.rules`)

La règle donne au groupe `backupbox` l'écriture sur `/sys/class/leds/ACT/{trigger,brightness}`. Sans elle, l'application fonctionne quand même, simplement sans témoin lumineux. Au repos, la DEL reprend son déclencheur d'origine (activité de la microSD).

---

## 5. Réseau : point d'accès Wi-Fi

```bash
sudo deploy/setup-hotspot.sh '<mot-de-passe-wifi-8-caractères-min>'
```

Le script crée la connexion NetworkManager `BackupBox-AP` :
- SSID `BackupBox`, WPA2-PSK (AES), bande 2,4 GHz, canal 6 ;
- adresse `10.42.0.1/24`, avec DHCP pour les clients (`ipv4.method shared`) ;
- démarrage automatique ;
- nom `backupbox.lan` résolu dans le point d'accès (dnsmasq de NetworkManager).

Adresses à utiliser depuis le téléphone :
- **`http://10.42.0.1`** : toujours fiable ;
- `http://backupbox.lan` : par le DNS du point d'accès ;
- `http://backupbox.local` : mDNS, qui demande `avahi-daemon`. Il est présent par défaut sur Pi OS, mais la prise en charge sur Android est inégale.

Le téléphone affichera « pas d'accès Internet » : il faut choisir de **rester connecté**. Sur Android, désactiver « basculer vers les données mobiles » pour ce réseau si l'interface ne charge pas.

Pour accéder au Pi à la maison, passer par Ethernet : SSH et mises à jour. Le point d'accès occupe `wlan0`.

L'application ne demande aucune authentification : **le mot de passe Wi-Fi est la seule barrière**. Ne jamais créer de point d'accès ouvert.

---

## 6. Réglages système conseillés (fiabilité terrain)

| Réglage | Commande | Pourquoi |
|---|---|---|
| Limiter le journal | dans `/etc/systemd/journald.conf` : `SystemMaxUse=64M` | Moins d'écritures sur la microSD |
| Couper le swap sur microSD (Bookworm) | `sudo systemctl disable --now dphys-swapfile` | Inutile pour < 150 Mo de RAM, use la microSD |
| Vérifier l'alimentation | `vcgencmd get_throttled`, qui doit renvoyer `throttled=0x0` pendant une copie | Une sous-tension provoque des déconnexions USB |
| Bluetooth (optionnel) | `dtoverlay=disable-bt` dans `/boot/firmware/config.txt` | Un peu moins de consommation |

### Problème UAS (adaptateurs USB-SATA)

Certains boîtiers SSD posent problème avec le pilote UAS sur Pi 4. Dans `dmesg`, cela se voit sous la forme `uas_eh_abort_handler` ou `reset SuperSpeed USB device`, avec des copies en échec. Pour le corriger :

```bash
lsusb                       # repérer l'identifiant du SSD, ex. 04e8:4001
sudo sed -i 's/$/ usb-storage.quirks=04e8:4001:u/' /boot/firmware/cmdline.txt   # une seule ligne !
sudo reboot
```

BackupBox détecte ces erreurs : la vérification échoue, puis l'erreur d'E/S est signalée. Mais la vraie solution est cette quirk.

---

## 7. Fonctionnement attendu (pour diagnostiquer)

1. Toutes les 2 s, le service liste les disques avec `lsblk -J -b`. Tout disque qui porte `/`, `/boot/firmware` ou le swap est **ignoré** : la microSD système n'apparaît jamais.
2. Une nouvelle partition est montée par `udisksctl mount -b /dev/sdX1 -o ro` dans `/media/backupbox/<ÉTIQUETTE>`.
3. Selon ce qu'elle contient :
   - `.backupbox/config.json` à la racine : c'est un **SSD de destination**. Il est remonté en lecture-écriture, et son UUID est mémorisé pour être monté directement en écriture la prochaine fois.
   - `DCIM/` ou `PRIVATE/M4ROOT` : c'est une **carte d'appareil**. Elle reste en lecture seule et sa sauvegarde démarre : tout de suite si la carte est déjà connue, après 30 s sinon.
   - Sinon, c'est un **disque inconnu**. Il n'est jamais sauvegardé automatiquement ; on peut l'initialiser comme SSD depuis l'interface.
4. La copie se fait en plusieurs temps :
   - lecture de la carte et calcul de l'empreinte xxh128 à la volée ;
   - écriture dans `SSD/.backupbox/tmp/` ;
   - `fsync`, puis vidage du cache de pages ;
   - relecture depuis le SSD et comparaison ;
   - si tout concorde : renommage vers `SSD/<projet>/Footage_RAW/<source>/<chemin sur la carte>`, et ajout d'une ligne au manifeste.
5. Une fois la sauvegarde vérifiée, la carte est démontée. La DEL reste allumée fixe, et la carte peut être retirée.

Fichiers utiles :
- journal : `journalctl -u backupbox -f` ;
- réglages et historique : `/var/lib/backupbox/{settings,state,history}.json` ;
- sur le SSD : `.backupbox/config.json`, `<projet>/.backupbox/manifests/<source>.jsonl` et `<projet>/.backupbox/reports/<id>.json`.

---

## 8. Mises à jour

```bash
cd ~/BackupBox && git pull && sudo deploy/install.sh
```

Il faut Internet (Ethernet), pour `apt` et `pip`. Le venv est conservé ; seul le code est remplacé.

---

## 9. Recette sur le matériel (à exécuter et consigner)

Chaque ligne doit donner le résultat attendu. Notez les résultats réels : ils remplacent la mention « non testé sur Pi ».

| # | Action | Résultat attendu |
|---|---|---|
| R1 | `systemctl is-active backupbox` | `active` |
| R2 | `curl -s http://127.0.0.1/api/health` | JSON avec `"version":"2.0.0"` |
| R3 | `id backupbox` | groupes `backupbox` et `video` |
| R4 | `ls -l /sys/class/leds/ACT/trigger` | groupe `backupbox`, droit `w` pour le groupe |
| R5 | Brancher un **SSD vierge** formaté en exFAT, puis `journalctl -u backupbox -n 20` | montage dans `/media/backupbox/…`, aucune erreur polkit |
| R6 | Interface : « Utiliser comme SSD de destination », projet `Test_Recette` | `.backupbox/config.json` et `Test_Recette/Footage_RAW/` créés sur le SSD |
| R7 | Insérer une carte GoPro ou DJI dans le lecteur | carte listée, décompte de 30 s, puis copie |
| R8 | Pendant la copie : `findmnt -no OPTIONS /media/backupbox/<ÉTIQUETTE_CARTE>` | contient `ro` |
| R9 | Pendant la copie : `vcgencmd get_throttled` | `throttled=0x0` |
| R10 | Fin de la copie | « Sauvegardée et vérifiée », carte démontée, DEL fixe |
| R11 | Comparer la carte et le SSD : `diff -r` (carte remontée en lecture seule) contre `…/Footage_RAW/<source>/` | aucune différence |
| R12 | Réinsérer la même carte | aucun décompte, « Déjà sauvegardée » |
| R13 | Retirer la carte **pendant** une copie | « La carte a été retirée… », DEL clignotante, rien dans `.backupbox/tmp/` après réinsertion |
| R14 | « Éjecter le SSD » dans l'interface | SSD hors tension (`lsblk` ne l'affiche plus) |
| R15 | Téléphone sur le Wi-Fi `BackupBox`, ouvrir `http://10.42.0.1` | l'interface s'affiche |
| R16 | Système → Éteindre | le Pi s'éteint proprement |
| R17 | Redémarrage complet | service actif et point d'accès visible en < 60 s |
| R18 | Débit : copier une carte d'au moins 20 Go, noter la durée affichée dans « Détails » | consigner les Mo/s, la référence de la carte et le lecteur |

---

## 10. Dépannage

| Symptôme | Cause probable | Vérification / correction |
|---|---|---|
| `Not authorized to perform operation` dans le journal | Règle polkit absente ou polkit trop ancien | `ls /etc/polkit-1/rules.d/50-backupbox.rules`, puis `systemctl restart polkit` |
| `udisksctl introuvable` | udisks2 non installé | `sudo apt install udisks2` |
| La carte apparaît « Montage impossible » | Système de fichiers corrompu ou inconnu | `sudo fsck.exfat -n /dev/sdX1` (lecture seule) ; ne jamais réparer une carte de rushes avant de l'avoir copiée |
| Aucune carte ne s'affiche | Lecteur sur un port USB 2 ou carte mal insérée | `lsblk`, `dmesg -w` à l'insertion |
| Carte montée en écriture | Un automonteur de bureau est installé | désinstaller le bureau ; utiliser Pi OS **Lite** |
| L'interface ne charge pas sur Android | Bascule vers les données mobiles | désactiver la bascule pour ce Wi-Fi ; utiliser `http://10.42.0.1` |
| Copies en échec, `uas_eh_abort_handler` dans `dmesg` | Pont USB-SATA incompatible UAS | quirk `usb-storage.quirks=…:u` (section 6) |
| Bannière « sous-tension » | Alimentation ou câble insuffisant | alimentation officielle 3 A ; hub alimenté pour le SSD |
| Port 80 refusé | Unité modifiée sans `AmbientCapabilities` | réinstaller avec `deploy/install.sh` |
| « Initialisation impossible : Permission denied » | SSD en ext4 (racine possédée par root) | `sudo chown backupbox: /media/backupbox/<ÉTIQUETTE>` ou reformater en exFAT |
