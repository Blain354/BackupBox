# BackupBox : configuration du Raspberry Pi

Ce guide s'adresse à l'agent (ou à la personne) qui prépare le Raspberry Pi. Il dit ce dont l'application a besoin au niveau du système, pourquoi, comment l'installer et comment vérifier que c'est fait. Les choix matériels (tampon, buzzer, bouton, boîtier) sont dans [MATERIEL.md](MATERIEL.md), les notes de conception dans [DESIGN.md](DESIGN.md).

> **État de validation.** L'application et ses 123 tests ont été validés en simulation, sur Linux x86-64. **Rien n'a encore tourné sur un vrai Pi.** La section 9 est la recette à exécuter sur le matériel. Tout écart y est un bogue à signaler, pas une étape à contourner.

---

## 0. Résumé pour un agent pressé

```bash
# Sur Raspberry Pi OS Lite 64 bits, avec Internet (Ethernet), en tant qu'administrateur :
sudo raspi-config nonint do_hostname backupbox
sudo raspi-config nonint do_wifi_country CA
sudo timedatectl set-timezone America/Toronto
git clone https://github.com/Blain354/BackupBox.git ~/BackupBox
cd ~/BackupBox
sudo deploy/install.sh                      # paquets, utilisateur, polkit, udev, tampon, service
sudoedit /etc/default/backupbox             # GPIO du buzzer/DEL/bouton s'ils sont câblés (MATERIEL.md §3)
sudo deploy/setup-hotspot.sh '<mot-de-passe-wifi>'   # point d'accès « BackupBox » + portail captif
sudo reboot
# puis exécuter la recette de la section 9
```

---

## 1. Ce dont l'application a besoin

| Besoin | Pourquoi | Comment c'est fourni |
|---|---|---|
| Python ≥ 3.11 et un venv | Code de l'application (FastAPI, uvicorn, xxhash, gphoto2, gpiozero) | `python3`, `python3-venv` ; venv `--system-site-packages` dans `/opt/backupbox/venv` |
| Lister les disques | Détecter cartes et SSD à chaud (sondage `lsblk` toutes les 2 s) | `util-linux` (installé par défaut) |
| Monter / démonter **sans root** | Cartes montées **en lecture seule**, SSD en lecture-écriture | `udisks2` + règle polkit pour l'utilisateur `backupbox` |
| Mettre le SSD hors tension | Éjection propre (vidage du cache du SSD) | `udisksctl power-off`, même règle polkit |
| Lire exFAT, FAT32, NTFS | Cartes SDXC (exFAT), SDHC (FAT32), SSD (exFAT ou NTFS) | noyau (exfat, vfat, ntfs3) + `exfatprogs`, `dosfstools`, `ntfs-3g` |
| Lire les appareils **branchés par câble** en MTP/PTP | GoPro, appareils photo : ils ne se montrent pas comme un disque | libgphoto2 (incluse dans le paquet python `gphoto2`) + règle udev `99-backupbox-cameras.rules` qui donne au groupe `backupbox` l'accès USB ; `libgphoto2-6` et `libmtp-common` apportent les listes d'appareils connus |
| Drones branchés par câble (DJI) | Ils se montrent comme un disque USB | rien de plus : udisks2 ; le numéro de série USB identifie le drone |
| Un tampon local | Vider un appareil sans SSD ; vidé vers le SSD dès qu'il est branché | dossier `BACKUPBOX_BUFFER_DIR` (défaut `/srv/backupbox/buffer`, propriétaire `backupbox`), réserve `BACKUPBOX_BUFFER_RESERVE_GB` |
| Buzzer, DEL de façade, bouton (optionnels) | « Ne débranche rien » sans téléphone ; démarrer / éteindre au bouton | GPIO via `gpiozero` + `python3-lgpio` (apt), groupe `gpio` ; broches dans `/etc/default/backupbox` |
| Notification à l'arrivée sur le Wi-Fi | Le téléphone affiche « Se connecter au réseau » et ouvre BackupBox | portail captif : `address=/#/10.42.0.1` dans le dnsmasq de NetworkManager + redirections dans l'application |
| Notifications push (optionnel) | Quand le Pi a Internet (Ethernet, Wi-Fi d'hôtel) | ntfy, URL dans Réglages → Téléphone |
| Écouter sur le port 80 | Le téléphone tape seulement `http://10.42.0.1` | `AmbientCapabilities=CAP_NET_BIND_SERVICE` dans l'unité systemd |
| Éteindre / redémarrer depuis l'interface | Arrêt propre sur le terrain (protège la carte microSD système) | règle polkit `org.freedesktop.login1.power-off` / `reboot` |
| Piloter la DEL verte (ACT) | Témoin sans téléphone : cœur = copie, fixe = fini, clignote vite = erreur | règle udev qui donne au groupe `backupbox` l'écriture sur `trigger` et `brightness` |
| Lire l'état d'alimentation | Avertir en cas de sous-tension (cause n° 1 des déconnexions USB) | `/sys/devices/platform/soc/soc:firmware/get_throttled` (repli : `vcgencmd`, groupe `video`) |
| Un réseau Wi-Fi propre au Pi | Utilisation en voyage, sans Internet | NetworkManager en mode point d'accès (`deploy/setup-hotspot.sh`) |
| Stocker réglages et historique | Survivre aux redémarrages | `/var/lib/backupbox` (`StateDirectory=` systemd) |

Ce que l'application **ne** demande **pas** : root, sudo, un montage automatique par le système, l'accès à Internet après l'installation. `/etc/fstab` ne sert que pour un SSD **interne** de tampon (MATERIEL.md, option B), jamais pour le SSD de destination ni les cartes.

---

## 2. Matériel

| Élément | Exigence | Remarque |
|---|---|---|
| Carte | **Raspberry Pi 4** (2, 4 ou 8 Go) | 2 Go suffisent : l'application utilise < 150 Mo de RAM |
| Alimentation | **Officielle 5,1 V / 3 A USB-C** | Le Pi 4 limite l'ensemble des ports USB à 1,2 A |
| Carte microSD système | **256 Go « High Endurance »** si elle porte aussi le tampon (option A de MATERIEL.md), sinon 16–32 Go A1/A2 | Avec le tampon dessus, la réserve de 10 Go protège l'OS |
| Lecteur de cartes | USB 3, sur un **port bleu** (USB 3) | UHS-II conseillé si les cartes le sont. Un seul lecteur suffit |
| Câbles USB-C ↔ USB-A | Câbles **de données**, courts | Pour brancher les drones et les GoPro directement ; un câble « charge seulement » ne montre rien |
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

1. installe `python3 python3-venv udisks2 polkitd exfatprogs ntfs-3g dosfstools util-linux rsync curl libgphoto2-6 libmtp-common python3-lgpio` ;
2. crée l'utilisateur système `backupbox` (sans connexion), ajouté aux groupes `video`, `gpio` et `plugdev` ;
3. copie le code dans `/opt/backupbox/app`, possédé par root et en lecture seule pour le service ;
4. crée le venv `/opt/backupbox/venv`, **séparé du code** (ce qui corrige le défaut de l'ancienne version où chaque mise à jour effaçait le venv), avec `--system-site-packages` pour que `gpiozero` trouve `lgpio` ;
5. installe `deploy/polkit/50-backupbox.rules` dans `/etc/polkit-1/rules.d/` ;
6. crée `/etc/default/backupbox` depuis `deploy/backupbox.env` s'il n'existe pas (tampon, broches GPIO, adresse du portail), puis crée le dossier du tampon, propriété de `backupbox` ;
7. installe les règles udev `99-backupbox-led.rules` (DEL verte) et `99-backupbox-cameras.rules` (accès USB aux GoPro et appareils photo), puis les applique ;
8. installe et démarre `backupbox.service`, puis vérifie `http://127.0.0.1/api/health`.

### 4.1 Unité systemd (`deploy/systemd/backupbox.service`)

Les points importants :

- `User=backupbox` et `NoNewPrivileges=yes` ;
- `AmbientCapabilities=CAP_NET_BIND_SERVICE`, seule capacité accordée, pour le port 80 ;
- `StateDirectory=backupbox`, d'où `/var/lib/backupbox` en 0750 ;
- `EnvironmentFile=-/etc/default/backupbox` : tampon, broches GPIO, adresse du portail ;
- `Wants=`/`After=udisks2.service`.

**Ne pas** ajouter `ProtectSystem=`, `PrivateTmp=`, `PrivateMounts=` ni `ProtectHome=`. Ces options placent le service dans son propre espace de montage. Selon la propagation, les montages faits ensuite par udisks2 sous `/media/backupbox/` peuvent alors ne pas être visibles.

### 4.2 Règle polkit (`deploy/polkit/50-backupbox.rules`)

Elle autorise **uniquement l'utilisateur `backupbox`**, et **uniquement** pour :
- les montages, démontages, éjections et mises hors tension udisks2. Les variantes `*-other-seat` sont nécessaires, car un service n'a pas de session ;
- `login1.power-off` et `login1.reboot`.

Polkit ≥ 0.106 (règles JavaScript) est requis. C'est le cas de Bookworm et des versions suivantes (paquet `polkitd`).

### 4.3 DEL verte (`deploy/udev/99-backupbox-led.rules`)

La règle donne au groupe `backupbox` l'écriture sur `/sys/class/leds/ACT/{trigger,brightness}`. Sans elle, l'application fonctionne quand même, simplement sans témoin lumineux. Au repos, la DEL reprend son déclencheur d'origine (activité de la microSD).

### 4.4 Appareils MTP/PTP (`deploy/udev/99-backupbox-cameras.rules`)

Les GoPro et la plupart des appareils photo branchés par câble passent par libgphoto2, qui écrit directement sur `/dev/bus/usb/…`. Par défaut, seul un utilisateur connecté en session y a accès. La règle donne ce droit au groupe `backupbox`, de trois façons :
- les appareils que libgphoto2/libmtp reconnaissent (`ID_GPHOTO2`, `ID_MTP_DEVICE`) ;
- les fabricants, par identifiant USB : GoPro `2672`, DJI `2ca3`, Insta360 `2e1a`, Sony, Canon, Nikon, Fujifilm, Panasonic, Olympus, Ricoh.

Pour vérifier qu'un appareil est vu, brancher par exemple la GoPro **allumée**, puis :

```bash
sudo -u backupbox /opt/backupbox/venv/bin/python -c "import gphoto2 as gp; print(gp.Camera.autodetect())"
```

Le résultat attendu ressemble à `[['GoPro HERO12 Black', 'usb:001,007']]`.

### 4.5 Tampon local et GPIO (`/etc/default/backupbox`)

| Variable | Défaut | Rôle |
|---|---|---|
| `BACKUPBOX_BUFFER_DIR` | `/srv/backupbox/buffer` | tampon utilisé sans SSD ; vide = pas de tampon |
| `BACKUPBOX_BUFFER_RESERVE_GB` | `10` | espace toujours laissé libre (protège l'OS sur microSD) |
| `BACKUPBOX_BUZZER_PIN` / `_LED_PIN` / `_BUTTON_PIN` | vides | GPIO BCM, par exemple `17`, `27`, `3` (câblage dans MATERIEL.md §3) |
| `BACKUPBOX_PORTAL_URL` | `http://10.42.0.1/` | page ouverte par le portail captif et les notifications |

**Tampon sur un SSD interne (option B).** Formater le SSD en ext4, puis le monter **par `/etc/fstab`** sur `/srv/backupbox/buffer`, avec `nofail`. Faire ensuite `sudo chown -R backupbox: /srv/backupbox/buffer`. Monté hors de `/media`, ce disque n'apparaît jamais comme un appareil dans l'interface.

Après toute modification : `sudo systemctl restart backupbox`.

---

## 5. Réseau : point d'accès Wi-Fi

```bash
sudo deploy/setup-hotspot.sh '<mot-de-passe-wifi-8-caractères-min>'
```

Le script crée la connexion NetworkManager `BackupBox-AP` :
- SSID `BackupBox`, WPA2-PSK (AES), bande 2,4 GHz, canal 6 ;
- adresse `10.42.0.1/24`, avec DHCP pour les clients (`ipv4.method shared`) ;
- démarrage automatique ;
- nom `backupbox.lan` résolu dans le point d'accès (dnsmasq de NetworkManager) ;
- **portail captif** : `address=/#/10.42.0.1`, tous les noms pointent vers le Pi. Pour le désactiver : `--no-portal`.

### Notification « dès que la boîte est en ligne »

Le portail captif est le seul mécanisme de notification qui marche **sans Internet**. Voici l'enchaînement :
1. le Pi démarre, et le point d'accès est actif en 30 à 60 s ;
2. le téléphone, qui a déjà enregistré le réseau `BackupBox`, s'y connecte seul ;
3. il teste Internet (`connectivitycheck.gstatic.com/generate_204` sur Android, `captive.apple.com` sur iOS) ;
4. le DNS du point d'accès renvoie le Pi, et l'application répond par une redirection vers `http://10.42.0.1/` ;
5. Android affiche la notification « Se connecter au réseau Wi-Fi » ; iOS ouvre directement la feuille de connexion ;
6. un toucher ouvre BackupBox sur l'action rapide (« Tout transférer »).

Le comportement exact dépend du téléphone : c'est la recette **R20**, à valider sur le Galaxy S24. Le réglage « Ouvrir BackupBox automatiquement… » (Réglages → Téléphone) coupe les redirections.

**Notifications push (ntfy)** : seulement quand le Pi a Internet (Ethernet, Wi-Fi d'hôtel). Installer l'application ntfy sur le téléphone, s'abonner à un sujet difficile à deviner, puis saisir `https://ntfy.sh/<sujet>` dans Réglages → Téléphone. Sur le point d'accès du Pi, sans Internet, ntfy ne peut rien envoyer : c'est le portail captif qui prend le relais.

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

1. Toutes les 2 s, le service liste les disques avec `lsblk -J -b`. Tout disque qui porte `/`, `/boot/firmware`, le swap ou un montage hors de `/media` (fstab, tampon interne) est **ignoré** : la microSD système n'apparaît jamais. Il relève aussi l'empreinte de `/sys/bus/usb/devices`. Si elle change, il interroge libgphoto2 pendant 12 s pour trouver une GoPro ou un appareil photo en MTP/PTP.
2. Une nouvelle partition est montée par `udisksctl mount -b /dev/sdX1 -o ro` dans `/media/backupbox/<ÉTIQUETTE>`.
3. Selon ce qu'elle contient :
   - `.backupbox/config.json` à la racine : c'est un **SSD de destination**. Il est remonté en lecture-écriture, et son UUID est mémorisé pour être monté directement en écriture la prochaine fois.
   - `DCIM/` ou `PRIVATE/M4ROOT` : c'est une **carte d'appareil**. Si le fabricant USB est celui d'un appareil (DJI, GoPro…), c'est l'**appareil lui-même branché par câble**, identifié par son numéro de série USB. La carte reste en lecture seule et sa sauvegarde démarre : tout de suite si l'appareil est déjà connu, après 30 s sinon.
   - Sinon, c'est un **disque inconnu**. Il n'est jamais sauvegardé automatiquement ; on peut l'initialiser comme SSD depuis l'interface.
4. Destination :
   - le **SSD** s'il est branché et a un projet ;
   - sinon le **tampon du Pi** (`BACKUPBOX_BUFFER_DIR`).

   Brancher le SSD pendant un décompte redirige la sauvegarde vers lui. Brancher le SSD quand le tampon contient des fichiers lance le **vidage** : copie vérifiée vers le SSD, puis effacement du tampon seulement.
5. La copie se fait en plusieurs temps :
   - lecture de la carte et calcul de l'empreinte xxh128 à la volée ;
   - écriture dans `SSD/.backupbox/tmp/` ;
   - `fsync`, puis vidage du cache de pages ;
   - relecture depuis le SSD et comparaison ;
   - si tout concorde : renommage vers `SSD/<projet>/Footage_RAW/<source>/<chemin sur la carte>`, et ajout d'une ligne au manifeste.
6. Une fois la sauvegarde vérifiée, la carte est démontée ou la session MTP fermée. La DEL reste allumée fixe, le buzzer joue trois bips courts, et l'appareil peut être débranché.

Fichiers utiles :
- journal : `journalctl -u backupbox -f` ;
- réglages et historique : `/var/lib/backupbox/{settings,state,history}.json` ;
- sur le SSD : `.backupbox/config.json`, `<projet>/.backupbox/manifests/<source>.jsonl` et `<projet>/.backupbox/reports/<id>.json` ;
- dans le tampon : la même organisation, plus `.backupbox/drained/<source>.jsonl`, le registre de ce qui a déjà été déplacé vers un SSD. Grâce à lui, un drone non formaté n'est pas recopié le lendemain.

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
| R3 | `id backupbox` | groupes `backupbox`, `video`, `gpio`, `plugdev` |
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
| R19 | Brancher l'**Avata 2** puis le **Mini 5 Pro** par câble, allumés, **sans SSD** | chaque drone est reconnu (« branché par câble »), part vers le « Tampon du Pi », dans deux dossiers distincts. Consigner le `lsblk -o NAME,VENDOR,MODEL,SERIAL` de chaque drone |
| R20 | Téléphone (S24) : réseau `BackupBox` enregistré, Wi-Fi activé, puis démarrer le Pi | notification « Se connecter au réseau » ; un toucher ouvre BackupBox. Noter le délai entre l'alimentation et la notification |
| R21 | Brancher le **SSD** pendant le décompte d'un drone | la destination passe à « SSD « … » » avant le démarrage |
| R22 | Avec le tampon rempli (R19), brancher le SSD | vidage automatique, buzzer actif pendant l'écriture, tampon « Rien en attente », fichiers présents sur le SSD |
| R23 | Rebrancher un drone non formaté, sans SSD | « Déjà sauvegardé », rien n'est recopié dans le tampon |
| R24 | Brancher une **GoPro** allumée par câble (MTP) | « GoPro HERO… branché par câble (MTP) », copie vérifiée. Consigner le débit (MTP est souvent plus lent qu'un lecteur de cartes) |
| R25 | Buzzer, DEL de façade, bouton câblés (MATERIEL.md §3) | double bip pendant l'écriture ; appui court = démarrage ; appui de 3 s = arrêt (refusé pendant une écriture) ; le même bouton rallume le Pi |
| R26 | Préréglage « Vidéos seulement » sur un drone avec `.LRF`/`.SRT` | seules les `.MP4` sont copiées ; le choix est retenu au branchement suivant |

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
| GoPro ou appareil photo branché mais invisible | Appareil éteint, câble « charge seulement », mode USB de la GoPro, ou droits USB | allumer l'appareil ; essayer un autre câble ; vérifier `ls -l /dev/bus/usb/*/*` (groupe `backupbox`) puis la commande de la §4.4 ; réappliquer la règle avec `sudo udevadm trigger` |
| Drone branché mais invisible | Drone éteint, ou mode « charge » | allumer le drone avec sa batterie ; `dmesg -w` doit montrer un disque `sdX` |
| Pas de notification à l'arrivée sur le Wi-Fi | Téléphone qui garde les données mobiles, portail désactivé | Réglages → Téléphone ; `cat /etc/NetworkManager/dnsmasq-shared.d/backupbox.conf` contient `address=/#/10.42.0.1` ; ouvrir `http://10.42.0.1` à la main |
| Pas de bip ni de DEL de façade | GPIO non configurés ou `lgpio` absent | `/etc/default/backupbox` ; `journalctl -u backupbox \| grep GPIO` ; `id backupbox` contient `gpio` |
| Le tampon ne se vide pas | Vidage précédent en échec (pas de boucle de réessai), ou vidage automatique coupé | bouton « Vider vers le SSD » ; débrancher et rebrancher le SSD |
