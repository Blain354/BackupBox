# BackupBox

Un Raspberry Pi 4 dans une boîte fermée qui vide les drones, GoPro et appareils photo, **sans laptop**, et **prouve** que chaque fichier est bien copié avant qu'on reformate.

**Une journée type :**

1. Sur le terrain, sans SSD : on branche le drone par son câble USB-C. Le téléphone affiche « Se connecter au réseau BackupBox ». Un toucher, puis « Tout transférer » : la copie part dans le **tampon du Pi**, vérifiée.
2. Une GoPro, un appareil photo (par câble ou carte dans un lecteur) : même chose. Chaque appareil est reconnu et va dans son propre dossier.
3. Le soir, on branche le **SSD** : le tampon s'y vide tout seul, vérifié, puis libère le Pi.
4. Tant qu'il ne faut rien débrancher, la boîte bipe (buzzer en option) et la DEL bat. Trois bips courts : c'est fini.

| Témoin | État |
|---|---|
| DEL qui bat + double bip | écriture en cours : ne rien débrancher |
| clignotement lent | un appareil attend votre confirmation |
| DEL fixe + 3 bips courts | sauvegarde vérifiée |
| clignotement rapide + 3 bips longs | erreur : regarder le téléphone |

Le SSD sort rangé pour [Footage Organizer](https://github.com/blain-projects/Footage_organizer_web) :

```
<projet>/Footage_RAW/<appareil>/DCIM/DJI_001/DJI_20260714_0001_D.MP4
```

## Ce qui est garanti

- **Vérification réelle.** Chaque fichier est relu depuis le SSD, cache vidé, puis comparé par empreinte xxh128 à ce qui a été lu sur la carte. Un fichier n'apparaît sous son nom final qu'une fois vérifié.
- **Appareil intouchable.** Il est lu en lecture seule, et rien n'y est jamais écrit ni effacé : on formate dans l'appareil quand le SSD a tout.
- **Tampon sûr.** Un fichier ne quitte le tampon du Pi qu'une fois vérifié sur le SSD, et un drone non formaté n'est jamais recopié.
- **Rien n'est écrasé.** Un nom identique avec un contenu différent donne `NOM_conflit-<hash>.ext`.
- **Reprise incrémentale.** Une carte réinsérée ne copie que ses nouveaux clips.
- **Robuste aux incidents.** Une carte ou un SSD retiré, un disque plein ou une coupure de courant produisent un message clair et jamais de fichier à moitié copié.

## Documentation

| Document | Pour qui |
|---|---|
| [docs/RASPBERRY-PI-SETUP.md](docs/RASPBERRY-PI-SETUP.md) | Préparer le Pi : OS, accès requis, service, Wi-Fi et portail captif, recette sur le matériel |
| [docs/MATERIEL.md](docs/MATERIEL.md) | Tampon (microSD ou SSD interne), buzzer, DEL, bouton, boîtier, alimentation |
| [docs/DESIGN.md](docs/DESIGN.md) | Problème, user stories et critères d'acceptation, architecture, décisions |

## Installation sur le Pi (résumé)

```bash
git clone https://github.com/Blain354/BackupBox.git ~/BackupBox && cd ~/BackupBox
sudo deploy/install.sh
sudoedit /etc/default/backupbox        # tampon, GPIO du buzzer/DEL/bouton
sudo deploy/setup-hotspot.sh '<mot-de-passe-wifi>'
```

Ensuite, depuis le téléphone : Wi-Fi `BackupBox` (la page s'ouvre seule), sinon **http://10.42.0.1**.

## Essayer sans Raspberry Pi (mode simulation)

Chaque sous-dossier d'un dossier de simulation joue le rôle d'un disque.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m backupbox --simulate ./sim/dev --state-dir ./sim/state --buffer-dir ./sim/buffer --port 8080 &
S=".venv/bin/python -m backupbox.simctl ./sim/dev"
$S card sdc1 --brand dji --usb-vendor DJI --usb-model "Avata 2" --usb-serial A1 --sidecars   # drone par câble
$S card mtp1 --brand gopro --mtp --usb-model "GoPro HERO12 Black" --usb-serial C1            # GoPro en MTP
$S card sdb1 --brand gopro --clips 5                                                          # carte dans un lecteur
$S ssd sda1 --label T7                                                                        # le SSD du soir
$S unplug sdc1                                                                                # débrancher
# ouvrir http://localhost:8080
```

## Tests

```bash
.venv/bin/python -m pytest      # 123 tests : moteur de copie, user stories, tampon, MTP, GPIO, API
tests/e2e/run.sh                # la journée de voyage au téléphone (Playwright)
```

## Désinstallation

```bash
sudo systemctl disable --now backupbox
sudo rm -rf /opt/backupbox /var/lib/backupbox
sudo rm -f /etc/systemd/system/backupbox.service /etc/polkit-1/rules.d/50-backupbox.rules \
  /etc/udev/rules.d/99-backupbox-led.rules /etc/udev/rules.d/99-backupbox-cameras.rules /etc/default/backupbox
sudo rm -rf /srv/backupbox   # le tampon : vérifier d'abord qu'il est vide
sudo systemctl daemon-reload && sudo userdel backupbox
```
