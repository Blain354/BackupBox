# BackupBox : notes de conception

## 1. Le problème

En voyage, Guillaume filme avec plusieurs appareils : drones DJI (Avata 2, Mini 5 Pro), GoPro, appareil photo, et ceux des amis (`Drone_Keven`, `Drone_Kyle`…). Une session de drone fait 50 à 75 Go. Il faut :

1. vider un appareil **sur le terrain**, sans laptop et sans forcément avoir le SSD, idéalement en branchant l'appareil lui-même par son câble USB-C ;
2. avoir la **preuve** que tout est bien copié avant de reformater la carte dans l'appareil ;
3. le soir, retrouver tout sur le **SSD de voyage**, sans manipulation, rangé comme **Footage Organizer** l'attend : `<projet>/Footage_RAW/<source>/…` ;
4. une boîte fermée qui traîne au fond du sac, qui **prévient d'elle-même** : notification sur le téléphone en arrivant sur son Wi-Fi, bips et DEL tant qu'il ne faut rien débrancher.

BackupBox est un Raspberry Pi 4 dans une boîte. Quand on branche un appareil, la sauvegarde se fait seule vers le SSD s'il est là, sinon vers le **tampon** du Pi. Ce tampon se vide tout seul dans le SSD dès qu'il est branché. Le téléphone sert à confirmer d'un toucher (« Tout transférer »), nommer un nouvel appareil, choisir quoi copier et suivre. Sans téléphone, le buzzer, la DEL et un bouton suffisent.

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
| US-19 | Je branche mon drone ou ma GoPro **directement par câble** ; BackupBox sait de quel appareil il s'agit. | Un drone qui se montre comme disque USB est reconnu par son fabricant et son modèle USB (« DJI Avata 2 »), et identifié **pour toujours** par son numéro de série USB, même carte reformatée. Deux drones vont dans deux dossiers. Un lecteur de cartes vendu par un fabricant d'appareils (Sony MRW-…) reste un lecteur. Une GoPro ou un appareil photo en **MTP/PTP** est lu par libgphoto2, en blocs de 8 Mio, avec la même vérification. | `test_field_workflow::test_us19_*`, `test_mtp`, `test_detect`, E2E |
| US-20 | Sans SSD sur le terrain, je vide le drone dans le Pi. | Sans SSD, la destination est le **tampon du Pi**. Une réserve (10 Go par défaut) protège l'OS ; un tampon trop plein refuse avant de copier. L'interface montre ce qui attend le SSD. | `test_us20_*`, E2E |
| US-21 | Si je branche le SSD pendant que BackupBox me propose de transférer, le SSD a la priorité. | Un décompte en cours vers le tampon passe au SSD au branchement, et inversement si le SSD est débranché. | `test_us21_*` |
| US-22 | Le soir, je branche le SSD : le tampon s'y vide seul. | Le vidage démarre seul (réglable) : copie vérifiée vers `<projet>/Footage_RAW/<source>/` sur le SSD, puis effacement **du tampon seulement**. Les fichiers déplacés sont notés dans un registre, pour qu'un drone non formaté ne soit pas recopié. Un SSD débranché ou plein ne fait rien perdre. Un échec n'entraîne pas de boucle de réessai ; un bouton « Vider vers le SSD » permet de relancer. | `test_us22_*`, E2E |
| US-23 | Je choisis de ne copier que les vidéos, ou de laisser les aperçus. | Préréglages « Tout », « Sans aperçus (.LRV .LRF .THM) » (les `.SRT` de télémétrie restent), « Vidéos seulement », mémorisés par appareil. L'interface affiche le contenu (vidéos, photos, annexes, taille) et la taille prévue avant le transfert. | `test_us23_*`, `test_api::test_presets_and_drain_endpoints`, E2E |
| US-24 | Mon téléphone me prévient quand la boîte est prête, et en fin de transfert. | En arrivant sur le Wi-Fi `BackupBox`, les tests de connectivité d'Android, iOS et Windows sont redirigés vers l'interface : le téléphone affiche « Se connecter au réseau ». Les noms locaux et les adresses IP ne sont jamais redirigés. Avec Internet, des notifications ntfy partent au branchement, à la fin et en cas d'échec, avec un lien vers l'interface. | `test_api::test_connectivity_checks_*`, `test_notify`, `test_us24_*` |
| US-25 | Sans téléphone, un bouton suffit. | Appui court : démarre ce qui attend. Appui de 3 s : arrêt propre, refusé pendant une écriture. | `test_us25_*`, `test_indicators::test_button_*` |
| US-26 | Tant qu'il ne faut rien débrancher, la boîte le dit. | Pendant l'écriture : double bip toutes les N s (1 s par défaut, réglable) et clignotement court. En attente de l'utilisateur : clignotement lent, 1 bip. Terminé : DEL fixe, 3 bips courts. Erreur : clignotement rapide, 3 bips longs. Tout se coupe dans les réglages. Bannière « ne débranchez rien » dans l'interface. | `test_us26_*`, `test_indicators`, `test_us13_*` |

Lancer les tests :

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 123 tests
tests/e2e/run.sh [dossier-captures]   # parcours Playwright (Node + Playwright requis)
```

---

## 3. Architecture

```
backupbox/
  devices.py      lsblk → périphériques candidats (disque système et montages fstab exclus)
  platform.py     LinuxPlatform (lsblk + udisksctl + gphoto2) | SimPlatform (dossiers = appareils)
  mtp.py          appareils MTP/PTP (GoPro, appareils photo) via libgphoto2
  detect.py       carte ou appareil ? marque, modèle, nom par défaut, identité
  destination.py  marque .backupbox, projets, Footage_RAW, nettoyage tmp
  copier.py       moteur de copie vérifiée + progression
  manifest.py     JSONL des fichiers vérifiés, par source
  manager.py      orchestrateur : sondage, minuteries, tampon et vidage, file, actions
  indicators.py   DEL ACT, DEL GPIO, buzzer, bouton (gpiozero, facultatif)
  notify.py       notifications ntfy (quand le Pi a Internet)
  settings.py     réglages, SSD connus, historique (/var/lib/backupbox)
  system.py       DEL, état d'alimentation, horloge, arrêt
  api.py          FastAPI : /api/* + interface statique + portail captif
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

**Destination d'une tâche « appareil »** : le SSD branché (avec un projet), sinon le tampon du Pi. Elle est choisie au moment où la tâche est armée, puis de nouveau quand un SSD apparaît ou disparaît.

**Tâche « vidage »** (une par projet et par source du tampon) : créée quand un SSD avec projet est branché et que le tampon contient des fichiers. Elle copie et vérifie vers le SSD. Ensuite, pour chaque fichier vérifié, elle ajoute une ligne au registre `tampon/.backupbox/drained/<source>.jsonl`, puis efface le fichier du tampon. Un projet provisoire « Sans_projet » (tampon rempli avant tout SSD) est versé dans le projet actif du SSD.

### Invariants

1. **Un fichier sous son nom final dans `Footage_RAW` a toujours été vérifié.** L'écriture se fait dans `.backupbox/tmp/`, suivie de `fsync`, de `POSIX_FADV_DONTNEED`, de la relecture et de la comparaison. Le renommage n'a lieu qu'ensuite (même système de fichiers, donc atomique), puis le manifeste est complété.
2. **La carte n'est jamais écrite.** Elle est montée `ro`, et le moteur n'ouvre la source qu'en lecture.
3. **Rien n'est jamais écrasé ni effacé** sur le SSD, hors `.backupbox/tmp/`.
4. **Le disque système n'existe pas pour l'application** : il est filtré dès `lsblk`.
5. **Un fichier n'est effacé du tampon qu'après sa vérification sur un SSD.** Il est noté dans le registre *avant* l'effacement.

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
| Effacer l'appareil après un transfert vers le tampon | **Non** | Il n'existe alors que deux copies, l'appareil et le Pi. Effacer l'appareil en laisserait une seule, au fond d'un sac. On formate dans l'appareil une fois le SSD rempli ; l'interface le dit. |
| Tampon local | Un dossier configurable (`BACKUPBOX_BUFFER_DIR`), traité comme une destination interne | Le même moteur, les mêmes manifestes, la même vérification. Le matériel est au choix (microSD, SSD interne) : voir MATERIEL.md. |
| Priorité SSD | Le SSD gagne toujours, y compris pendant un décompte | C'est la demande : « le SSD doit prendre priorité ». |
| Registre de vidage global au tampon | `.backupbox/drained/<source>.jsonl` à la racine du tampon | Le projet du tampon suit celui du SSD : un registre rangé par projet raterait les fichiers déjà vidés (bogue trouvé par `test_us22_drained_clips_*`). |
| Identité d'un appareil branché par câble | `usb:<marque>:<n° de série USB>` | Stable même après un formatage. Les lecteurs de cartes donnent leur propre numéro : seul un fabricant d'appareil, hors lecteurs, est pris pour l'appareil. |
| MTP/PTP | libgphoto2 (paquet `gphoto2`, roue aarch64 qui inclut la bibliothèque) | GoPro et appareils photo ne se montrent pas comme disque. L'empreinte est calculée sur les octets reçus par USB, et la relecture vérifie le disque. Pas de seconde lecture de l'appareil : elle doublerait le temps sur un lien déjà lent. |
| Notification hors ligne | Portail captif (DNS joker + redirection des tests de connectivité) | Sans Internet, aucun service push n'est joignable. Le portail est le seul mécanisme que les téléphones affichent seuls. On ne prétend jamais avoir Internet (pas de `204`) : le téléphone garde ses données mobiles. |
| Notification en ligne | ntfy (JSON, tâche de fond, sans réessai) | Gratuit, application Android/iOS, auto-hébergeable. Un échec réseau ne ralentit jamais une sauvegarde. |
| Buzzer | Double bip toutes les N s pendant l'écriture (1 s par défaut, demandé) | Avis : 5–10 s est plus vivable sur une heure de copie ; c'est réglable et se coupe. |

---

## 5. Limites connues et suites possibles

- **Jamais exécuté sur un vrai Pi à ce jour.** Les points à confirmer par la recette :
  - le format exact de la sortie `udisksctl` (un repli via `lsblk` est prévu) ;
  - les droits polkit sans session ;
  - la propagation des montages vers le service ;
  - les permissions de la DEL ;
  - le débit réel.
- Le MTP/PTP, l'identification des drones par l'USB, le portail captif et les GPIO n'ont été testés qu'avec des simulations : fausse caméra gphoto2, broches simulées par gpiozero, requêtes HTTP avec en-tête `Host`. Les recettes R19 à R26 sont là pour ça.
- Les téléphones (MTP) peuvent être vus comme des appareils photo par libgphoto2. Ce cas n'a pas été visé.
- Pas de réaffectation après coup d'une sauvegarde vers une autre source. Il faut renommer à la main, en tenant compte du manifeste.
- L'heure du Pi 4 peut être fausse sans réseau ; seules les dates de l'historique en souffrent.
- Idées :
  - miroir vers deux SSD ;
  - écran e-ink ou OLED ;
  - « Libérer l'appareil » une fois **deux** copies vérifiées (tampon + SSD), si le formatage dans l'appareil s'avère trop pénible ;
  - export MHL ;
  - mise à jour hors ligne par clé USB ;
  - affectation de la source par appareil photo (modèle EXIF).
