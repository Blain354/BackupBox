# BackupBox : notes de conception

## 1. Le problème

En voyage, Guillaume filme avec plusieurs appareils : GoPro, drones DJI (Avata 2, Mini 4), et ceux des amis (`Drone_Keven`, `Drone_Kyle`…). Les cartes se remplissent, et il faut :

1. vider chaque carte vers le **SSD de voyage**, sans laptop ;
2. avoir la **preuve** que tout est bien copié avant de reformater la carte dans l'appareil ;
3. retrouver au retour un SSD rangé comme **Footage Organizer** l'attend : `<projet>/Footage_RAW/<source>/…`.

BackupBox est un Raspberry Pi 4 dans une boîte : on branche le SSD, on insère une carte, la sauvegarde se fait seule. Le téléphone, connecté au Wi-Fi du Pi, sert à suivre, nommer la source et éjecter. Sans téléphone, la DEL verte dit où on en est.

### Pourquoi l'ancienne version (août 2025) était inutilisable

Elle avait été générée par Codex comme un explorateur de fichiers générique, sans user story ni essai sur le matériel :

- l'application **ne démarrait pas** sous systemd (`from transfer import …` dans un paquet) ;
- `install.sh` installait un paquet `lsblk` inexistant, et le service tournait sous l'utilisateur `pi`, absent par défaut ;
- rien ne montait les cartes sur Pi OS Lite, et l'éjection demandait root ;
- `update.sh` effaçait le venv ;
- `rsync -a` vers exFAT signalait une erreur même quand la copie avait réussi ;
- **aucune vérification**, et « Couper » effaçait la carte sans preuve ;
- n'importe qui sur le Wi-Fi pouvait déplacer `/home` ;
- il n'y avait aucun test.

La leçon retenue pour cette version : un seul cas d'usage, des critères d'acceptation testés, et une recette sur le matériel ([RASPBERRY-PI-SETUP.md §9](RASPBERRY-PI-SETUP.md#9-recette-sur-le-matériel-à-exécuter-et-consigner)).

### Alternative existante

[Little Backup Box](https://github.com/dmpop/little-backup-box) fait déjà « carte → disque sur un Pi ». BackupBox se justifie par trois choses qu'elle ne fait pas :
- la vérification par relecture, avec un manifeste d'empreintes ;
- la reprise incrémentale par carte ;
- l'arborescence directement consommable par Footage Organizer.

---

## 2. User stories et critères d'acceptation

Chaque critère est couvert par un test automatisé. Les fichiers sont dans `tests/` ; « E2E » désigne `tests/e2e/ui-flow.mjs` (Playwright, téléphone 390 px).

| # | User story | Critères d'acceptation | Tests |
|---|---|---|---|
| US-01 | Je branche un SSD neuf et je le déclare SSD de sauvegarde depuis le téléphone, avec un projet. | Un SSD vierge apparaît comme « disque non reconnu », monté en lecture seule. L'initialisation crée `.backupbox/config.json` et `<projet>/Footage_RAW/` sans rien effacer. Un SSD déjà initialisé est reconnu à sa marque, puis monté directement en écriture la fois suivante. Une carte d'appareil ne peut pas devenir destination. | `test_manager::test_us01_*`, `test_destination::test_init_*`, E2E |
| US-02 | Je choisis ou change le projet (voyage) actif. | Les dossiers contenant `Footage_RAW` (toute casse, 3 niveaux) sont proposés. Les noms dangereux (`..`, `.cache`, `x:y`, `Footage_RAW`) sont refusés. Sans projet, les cartes attendent. | `test_manager::test_us02_*`, `test_destination::test_validate_project`, `test_list_projects_and_sources` |
| US-03 | J'insère une carte et la sauvegarde démarre seule. | La carte est reconnue (GoPro, DJI, Insta360, Sony, DCIM générique) et montée **en lecture seule**. Une carte inconnue part après un décompte (30 s par défaut), une carte connue tout de suite. Sans SSD, elle attend. Un disque sans `DCIM` n'est **jamais** sauvegardé automatiquement. En mode manuel, il faut appuyer sur « Sauvegarder ». Un échec de montage est affiché. | `test_manager::test_us03_*`, `test_detect`, E2E |
| US-04 | Pendant le décompte, je change la source (ex. `GoPro_Keven`) ; la carte est ensuite mémorisée. | « Changer la source » met en pause. Le nom est validé (pas d'espace ni d'accent). La correspondance carte → source est écrite sur le SSD. La même caméra, même reformatée (numéro de série GoPro), revient dans le même dossier sans décompte. | `test_manager::test_us04_*`, `test_api::test_full_flow_over_http`, E2E |
| US-05 | La copie est vérifiée et rangée pour Footage Organizer. | `<projet>/Footage_RAW/<source>/<chemin sur la carte>` ; date de modification conservée ; carte inchangée octet pour octet. Chaque fichier est relu **depuis le disque** (cache vidé) et comparé par xxh128. Un rapport JSON est écrit sur le SSD et l'historique est mis à jour. | `test_copier::test_copy_is_verified_*`, `test_verification_reads_from_disk_not_page_cache`, `test_manager::test_us05_*` |
| US-06 | Je réinsère une carte : seuls les nouveaux clips sont copiés. | Les fichiers déjà au manifeste (même chemin, taille, date ± 2 s) sont sautés. Un fichier identique déjà présent mais absent du manifeste est comparé, jamais dupliqué. Un nom identique au contenu différent (carte reformatée) donne `NOM_conflit-<hash>.ext` : les deux versions sont gardées, et rien n'est jamais écrasé. | `test_copier::test_second_pass_*`, `test_identical_file_*`, `test_name_conflict_*`, `test_manager::test_us06_*` |
| US-07 | J'éjecte proprement carte et SSD depuis le téléphone. | Après une sauvegarde vérifiée, la carte est démontée automatiquement et n'est pas remontée tant qu'elle reste insérée. Le SSD est démonté **et** mis hors tension. L'éjection est refusée pendant une copie. | `test_manager::test_us07_*`, E2E |
| US-08 | Le SSD est trop petit. | Le refus a lieu **avant** toute copie, avec un message « X nécessaires, Y disponibles ». Un disque plein en cours de route arrête la tâche. | `test_copier::test_not_enough_space_*`, `test_ssd_full_aborts`, `test_manager::test_us08_*` |
| US-09 | Je retire la carte ou le SSD pendant la copie. | La tâche échoue avec un message clair (« Réinsérez-la : seuls les fichiers manquants seront copiés »). Aucun fichier partiel ne porte de nom final. Une tâche pas encore commencée est annulée. | `test_copier::test_card_removed_*`, `test_manager::test_us09_*` |
| US-10 | Coupure de courant pendant une copie. | Les fichiers temporaires sont supprimés au prochain montage du SSD. Une ligne de manifeste tronquée est ignorée. | `test_destination::test_clean_tmp_*`, `test_copier::test_manifest_ignores_torn_last_line`, `test_manager::test_us10_*` |
| US-11 | Je consulte l'historique et le détail d'une sauvegarde. | Liste des tâches ; détail avec les compteurs (copiés, déjà présents, conflits, échecs), la durée, le mode de vérification et la liste des fichiers à surveiller. | `test_api`, E2E |
| US-12 | Le système et les cartes sont protégés. | Le disque qui porte `/` n'est jamais listé, y compris quand le Pi démarre sur un SSD USB. Les cartes sont montées `ro` et la copie n'écrit jamais dessus. Aucune route de l'API ne prend de chemin arbitraire. | `test_devices`, `test_copier::test_copy_*never_touches_card`, `test_manager::test_us05_*` |
| US-13 | Sans téléphone, la DEL me dit où on en est. | Cœur = décompte ou copie ; fixe = sauvegarde vérifiée ; clignotement rapide = erreur ; activité microSD = repos. Une DEL sans permission ne bloque jamais. | `test_manager::test_us13_*`, `test_platform::test_led_*` |
| US-14 | Je règle le comportement. | Sauvegarde automatique, décompte (0–600 s), éjection automatique, vérification par relecture, revérification stricte, DEL. Les réglages persistent, et les valeurs invalides sont refusées en français. | `test_manager::test_us14_*`, `test_api::test_settings_endpoint` |
| US-15 | J'annule une sauvegarde. | Une tâche en attente est annulée sans redémarrer seule. Une tâche en cours s'arrête au prochain bloc de 8 Mio, et les fichiers déjà vérifiés restent. | `test_copier::test_cancel_*`, `test_manager::test_us15_*` |
| US-16 | J'éteins la boîte depuis le téléphone. | Arrêt ou redémarrage via logind (polkit). Refus pendant une copie. | `test_manager::test_us16_*`, `test_api::test_manual_backup_and_poweroff` |
| US-17 | Je suis prévenu d'une alimentation insuffisante. | Bannière rouge (sous-tension en cours) ou orange (depuis le démarrage), d'après `get_throttled`. | `test_platform::test_power_status_flags` |
| US-18 | Une erreur de lecture ou d'écriture ne passe pas inaperçue. | Une relecture qui ne concorde pas est réessayée une fois, puis le fichier est marqué en échec, sans nom final. La tâche est alors « incomplète » : « Ne formatez pas la carte ». Un fichier modifié pendant la copie est signalé. | `test_copier::test_verification_mismatch_*`, `test_transient_*`, `test_file_changing_*`, `test_unreadable_file_fails_alone` |

Lancer les tests :

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 83 tests
tests/e2e/run.sh [dossier-captures]   # parcours Playwright (Node + Playwright requis)
```

---

## 3. Architecture

```
backupbox/
  devices.py      lsblk → périphériques candidats (disque système exclu)
  platform.py     LinuxPlatform (lsblk + udisksctl) | SimPlatform (dossiers = disques)
  detect.py       carte d'appareil ? marque, source par défaut, identité de la carte
  destination.py  marque .backupbox, projets, Footage_RAW, nettoyage tmp
  copier.py       moteur de copie vérifiée + progression
  manifest.py     JSONL des fichiers vérifiés, par source
  manager.py      orchestrateur : sondage, minuteries, file, actions, instantané
  settings.py     réglages, SSD connus, historique (/var/lib/backupbox)
  system.py       DEL, état d'alimentation, horloge, arrêt
  api.py          FastAPI : /api/* + interface statique
  simctl.py       brancher ou débrancher des faux disques (mode simulation)
  static/         interface mobile (HTML/CSS/JS sans dépendance)
```

**Fils d'exécution :**
- le **sondeur** (toutes les 2 s) détecte les ajouts et retraits, monte, classe, fait avancer les décomptes et pilote la DEL ;
- le **travailleur** exécute une sauvegarde à la fois, dans l'ordre d'arrivée ;
- les requêtes HTTP lisent un instantané ou appellent une action.

Les trois partagent un verrou. Le montage, qui est lent, se fait hors verrou.

**Cycle de vie d'une tâche :**

```
                ┌──────── waiting (pas de SSD / pas de projet) ◄──┐
nouvelle carte ─┤                                                 │ SSD ou projet change
                └─► countdown ──(0 s si carte connue)──► queued ──► running ─► done | partial | failed | cancelled
                        │ « Changer la source »              ▲
                        └──────────► held ──── Démarrer ─────┘
```

`done` = tout vérifié ; `partial` = au moins un fichier en échec ; `failed` = tâche interrompue (carte ou SSD retiré, espace insuffisant).

### Invariants

1. **Un fichier sous son nom final dans `Footage_RAW` a toujours été vérifié.** L'écriture se fait dans `.backupbox/tmp/`, suivie de `fsync`, de `POSIX_FADV_DONTNEED`, de la relecture et de la comparaison. Le renommage n'a lieu qu'ensuite (même système de fichiers, donc atomique), puis le manifeste est complété.
2. **La carte n'est jamais écrite.** Elle est montée `ro`, et le moteur n'ouvre la source qu'en lecture.
3. **Rien n'est jamais écrasé ni effacé** sur le SSD, hors `.backupbox/tmp/`.
4. **Le disque système n'existe pas pour l'application** : il est filtré dès `lsblk`.

### Organisation sur le SSD

```
SSD/
├── .backupbox/
│   ├── config.json          {id, project, cards: {identité: source}}
│   └── tmp/                 copies en cours (nettoyé au montage)
└── Islande_2026/            ← « projet » = racine que l'on donne à Footage Organizer
    ├── Footage_RAW/
    │   ├── GoPro/DCIM/100GOPRO/GX010001.MP4
    │   └── GoPro_Keven/DCIM/100GOPRO/GX010001.MP4
    └── .backupbox/
        ├── manifests/GoPro.jsonl
        └── reports/20261027-142301-a1b2.json
```

Footage Organizer accepte les sous-dossiers de carte (`GoPro/DCIM/100GOPRO/`). Les dossiers `.backupbox` restent hors de `Footage_RAW`. Le nom de la source devient le préfixe des fichiers et le groupe de couleur dans DaVinci.

---

## 4. Décisions

| Décision | Choix | Raison / compromis |
|---|---|---|
| Effacer la carte (« Couper ») | **Jamais** | Le formatage dans l'appareil est plus sûr et recrée sa structure. C'est la principale source de perte de l'ancienne version. |
| Vérification | Relecture complète depuis le SSD, xxh128 | xxHash est l'algorithme des outils de déchargement de rushes (ASC MHL, Hedge) et coûte peu en calcul. La relecture ajoute une lecture complète de chaque fichier sur le SSD : le coût réel est mesuré par la recette R18. On peut la désactiver dans les réglages, et le résultat l'indique alors. |
| Moteur de copie | Python maison avec lecture en tâche de fond | `rsync` ne vérifie pas après écriture et échoue sur les droits exFAT. Lecture et écriture se chevauchent (file de 4 blocs de 8 Mio). |
| Montage | udisks2 + polkit | Pas de root ni de `sudoers` ; `ro` garanti pour les cartes ; mise hors tension du SSD. |
| Détection | Sondage `lsblk` toutes les 2 s | Plus simple et plus testable que les événements udev ; un seul processus court toutes les 2 s. |
| Identité de carte | Numéro de série GoPro (`MISC/version.txt`), sinon UUID du système de fichiers | Une carte reformatée change d'UUID. Pour les autres marques, une carte reformatée est vue comme nouvelle : le décompte laisse le temps de choisir la source, et le nom par défaut est la marque (même dossier qu'avant). |
| Conflit de nom | `NOM_conflit-<8 premiers caractères du hash>.ext` | Les deux versions sont gardées. Le préfixe garde l'ordre des compteurs de la caméra pour Footage Organizer. |
| Saut des fichiers déjà copiés | Manifeste (chemin, taille, date ± 2 s) + existence et taille sur le SSD | Rapide. La revérification stricte (réglage) recalcule les empreintes. |
| Interface | HTML/CSS/JS sans framework ni CDN, sondage à 1 s | Fonctionne hors ligne et résiste aux pertes Wi-Fi. Les cartes ne sont reconstruites que si leur structure change, pour que les boutons restent utilisables. |
| Authentification | Aucune ; le mot de passe WPA2 du point d'accès est la barrière | Boîte personnelle, réseau privé. L'API n'accepte aucun chemin arbitraire. |
| Port | 80 via `CAP_NET_BIND_SERVICE` | URL la plus simple au téléphone, sans root. |
| Une destination à la fois | Oui (préférence mémorisée si plusieurs) | La copie vers deux SSD (miroir) est reportée. |

---

## 5. Limites connues et suites possibles

- **Jamais exécuté sur un vrai Pi à ce jour.** Les points à confirmer par la recette :
  - le format exact de la sortie `udisksctl` (un repli via `lsblk` est prévu) ;
  - les droits polkit sans session ;
  - la propagation des montages vers le service ;
  - les permissions de la DEL ;
  - le débit réel.
- Les téléphones (MTP) ne sont pas des périphériques de stockage USB : non pris en charge.
- Pas de réaffectation après coup d'une sauvegarde vers une autre source. Il faut renommer à la main, en tenant compte du manifeste.
- L'heure du Pi 4 peut être fausse sans réseau ; seules les dates de l'historique en souffrent.
- Idées :
  - miroir vers deux SSD ;
  - écran e-ink ou OLED ;
  - bouton physique d'éjection ;
  - export MHL ;
  - mise à jour hors ligne par clé USB ;
  - affectation de la source par appareil photo (modèle EXIF).
