# BackupBox

Un Raspberry Pi 4 qui vide les cartes de caméra (GoPro, drones DJI, Insta360, appareils photo) vers le SSD de voyage, **sans laptop**, et **prouve** que chaque fichier est bien copié avant qu'on reformate la carte.

1. On branche le SSD.
2. On insère une carte.
3. La sauvegarde démarre seule. Le téléphone, connecté au Wi-Fi `BackupBox`, sert à suivre la progression, nommer la source et éjecter. Sans téléphone, la DEL verte du Pi indique l'état :

| DEL verte | État |
|---|---|
| cœur | copie en cours |
| fixe | sauvegarde vérifiée |
| clignote vite | erreur |

Le SSD sort rangé pour [Footage Organizer](https://github.com/blain-projects/Footage_organizer_web) :

```
<projet>/Footage_RAW/<source>/DCIM/100GOPRO/GX010001.MP4
```

## Ce qui est garanti

- **Vérification réelle.** Chaque fichier est relu depuis le SSD, cache vidé, puis comparé par empreinte xxh128 à ce qui a été lu sur la carte. Un fichier n'apparaît sous son nom final qu'une fois vérifié.
- **Carte intouchable.** Elle est montée en lecture seule, et rien n'y est jamais écrit ni effacé.
- **Rien n'est écrasé.** Un nom identique avec un contenu différent donne `NOM_conflit-<hash>.ext`.
- **Reprise incrémentale.** Une carte réinsérée ne copie que ses nouveaux clips.
- **Robuste aux incidents.** Une carte ou un SSD retiré, un disque plein ou une coupure de courant produisent un message clair et jamais de fichier à moitié copié.

## Documentation

| Document | Pour qui |
|---|---|
| [docs/RASPBERRY-PI-SETUP.md](docs/RASPBERRY-PI-SETUP.md) | Préparer le Pi : OS, accès requis, service, Wi-Fi, recette sur le matériel |
| [docs/DESIGN.md](docs/DESIGN.md) | Problème, user stories et critères d'acceptation, architecture, décisions |

## Installation sur le Pi (résumé)

```bash
git clone https://github.com/Blain354/BackupBox.git ~/BackupBox && cd ~/BackupBox
sudo deploy/install.sh
sudo deploy/setup-hotspot.sh '<mot-de-passe-wifi>'
```

Ensuite, depuis le téléphone : Wi-Fi `BackupBox`, puis **http://10.42.0.1**.

## Essayer sans Raspberry Pi (mode simulation)

Chaque sous-dossier d'un dossier de simulation joue le rôle d'un disque.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m backupbox --simulate ./sim/dev --state-dir ./sim/state --port 8080 &
.venv/bin/python -m backupbox.simctl ./sim/dev ssd sda1 --label T7                   # brancher un SSD
.venv/bin/python -m backupbox.simctl ./sim/dev card sdb1 --brand gopro --clips 5    # insérer une carte
.venv/bin/python -m backupbox.simctl ./sim/dev unplug sdb1                          # la retirer
# ouvrir http://localhost:8080
```

## Tests

```bash
.venv/bin/python -m pytest      # 83 tests : moteur de copie, user stories, API, plateforme
tests/e2e/run.sh                # parcours complet au téléphone (Playwright)
```

## Désinstallation

```bash
sudo systemctl disable --now backupbox
sudo rm -rf /opt/backupbox /var/lib/backupbox
sudo rm -f /etc/systemd/system/backupbox.service /etc/polkit-1/rules.d/50-backupbox.rules /etc/udev/rules.d/99-backupbox-led.rules
sudo systemctl daemon-reload && sudo userdel backupbox
```
