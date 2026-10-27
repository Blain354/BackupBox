# BackupBox : matériel, tampon local et témoins physiques

Ce document répond à trois questions concrètes : où stocker le tampon du Pi, comment la boîte prévient sans téléphone, et ce qu'il faut prévoir pour la mettre dans un boîtier fermé au fond d'un sac.

> Les débits ci-dessous sont des ordres de grandeur publics pour un Raspberry Pi 4, **pas des mesures sur ton matériel**. La recette R18 de [RASPBERRY-PI-SETUP.md](RASPBERRY-PI-SETUP.md) sert à les mesurer. Le débit USB réel de l'Avata 2 et du Mini 5 Pro branchés par câble est inconnu tant qu'il n'a pas été mesuré.

---

## 1. Le tampon local : faut-il une microSD de 256 Go ?

### À quoi sert le tampon

Il permet de vider un drone **sans avoir le SSD sur soi**. Le soir, quand le SSD est branché, le tampon s'y déverse tout seul : copie vérifiée, puis effacement du Pi seulement.

Il faut être honnête sur sa vraie valeur. Si tu traînes toujours le SSD avec le Pi, le tampon ne sert à rien : branche le SSD et c'est fini. Il prend tout son sens dans un cas précis. Le SSD maître (tout le voyage) reste à l'Airbnb. Les rushes du jour existent alors en deux endroits, la carte du drone et le Pi, jusqu'au soir. Un vol de sac ne coûte qu'une journée, pas le voyage.

### Les trois options

| | A. microSD 256 Go « High Endurance » | B. SSD interne USB + microSD système | C. Démarrer sur un SSD interne (sans microSD) |
|---|---|---|---|
| Matériel en plus | aucun (la carte remplace celle du système) | un petit SSD USB 3 dans le boîtier | un petit SSD USB 3 dans le boîtier |
| Écriture dans le tampon | ≈ 20–40 Mo/s : le lecteur microSD du Pi 4 plafonne vers 40–50 Mo/s | ≈ 200–350 Mo/s : la source devient le goulot | idem B |
| 60 Go dans le tampon (copie + relecture) | ≈ 45–60 min | limité par le drone | idem B |
| Vidage de 60 Go vers le SSD | ≈ 25 min (lecture microSD) | quelques minutes | idem B |
| Ports USB 3 libres | 2 | 1 : drone ou lecteur sur un port USB 2 (≈ 35–40 Mo/s), ou un hub USB 3 alimenté | 1 (idem B) |
| Robustesse | OS et tampon sur la même carte : la réserve de 10 Go protège l'OS ; une coupure pendant une écriture touche la carte système | OS séparé du tampon | aucune microSD fragile ; le meilleur pour « au fond du sac » |
| Configuration | `BACKUPBOX_BUFFER_DIR=/srv/backupbox/buffer` (défaut) | SSD monté sur `/srv/backupbox/buffer` par `/etc/fstab` | démarrage USB (EEPROM du Pi 4), tampon sur une 2ᵉ partition |

**Ma recommandation : commencer par A.** Prends une carte « High Endurance » (gamme vidéosurveillance), pas une carte photo rapide. Le seul coût est l'achat de la carte. Si tes drones sont en USB 2 par câble, ce qui est probable mais à mesurer, ils ne dépasseront de toute façon pas ~35–40 Mo/s : le tampon microSD ne sera alors pas le goulot. Passe à C si la recette R18 montre que l'attente est pénible.

Le logiciel est le même dans les trois cas : seul le dossier `BACKUPBOX_BUFFER_DIR` change. Un disque monté par `/etc/fstab` en dehors de `/media` n'apparaît jamais comme « appareil » dans l'interface.

### Usure

Deux sessions de 50–75 Go par jour représentent environ 100–150 Go écrits par jour. Les cartes « High Endurance » sont faites pour l'écriture vidéo continue : c'est leur usage normal. Une carte photo ordinaire s'userait nettement plus vite.

---

## 2. Effacer le drone après le transfert ? Non, et voici pourquoi

BackupBox **ne supprime jamais rien sur l'appareil**, et c'est voulu.

- Juste après un transfert vers le tampon, il n'existe que **deux** copies : l'appareil et le Pi. Effacer l'appareil à ce moment en laisse **une seule**, sur une microSD au fond d'un sac. C'est l'inverse d'une sauvegarde.
- **Formater dans l'appareil** (DJI Fly, menu de la GoPro) prend deux touches. C'est aussi ce que recommandent les fabricants : la carte retrouve une structure propre.
- L'interface dit quand c'est sûr. Après un transfert vers le tampon : « Gardez une copie sur l'appareil tant que le tampon n'est pas vidé vers le SSD ». Après un transfert vers le SSD : « vous pouvez formater la carte ».

« Supprimer les autres fichiers » est couvert par les préréglages. **Vidéos seulement** ne copie pas les fichiers annexes. **Sans aperçus** ne copie pas `.LRV`, `.LRF` et `.THM` mais garde les `.SRT` DJI, qui contiennent la télémétrie (GPS, heure) utile pour dater les plans. Ce choix est mémorisé par appareil.

---

## 3. Témoins physiques : le Pi 4 n'a ni haut-parleur ni buzzer

Ce qui existe d'origine :
- la **DEL verte (ACT)** : BackupBox la pilote déjà (cœur = écriture, fixe = terminé, clignotement rapide = erreur, lent = en attente de toi) ;
- la **DEL rouge (PWR)** : alimentation ;
- une prise audio 3,5 mm, inutilisable sans haut-parleur amplifié.

Dans un boîtier fermé, ces DEL ne se voient qu'avec un **guide de lumière** (tige acrylique ou PETG translucide imprimée en face des DEL).

### Ajouts conseillés (≈ 5 $ de pièces)

| Élément | Broche (BCM) | Broche physique | Câblage |
|---|---|---|---|
| Buzzer **actif** 3,3–5 V | GPIO 17 | 11 | Le plus simple : un module « active buzzer » avec transistor, alimenté en 3,3 V (broche 1), GND (broche 9), signal sur la broche 11. Un buzzer nu tire ~25–30 mA : il faut alors un transistor NPN (2N2222 + 1 kΩ sur la base), pas un GPIO en direct |
| DEL de façade | GPIO 27 | 13 | GPIO 27 → résistance 330 Ω → anode ; cathode → GND (broche 14) |
| Bouton poussoir (NO) | GPIO 3 | 5 | Entre la broche 5 et GND (broche 6), sans résistance (rappel interne). GPIO 3 **rallume aussi le Pi** après un arrêt |

Puis, dans `/etc/default/backupbox` :

```
BACKUPBOX_BUZZER_PIN=17
BACKUPBOX_LED_PIN=27
BACKUPBOX_BUTTON_PIN=3
```

### Ce que la boîte dit

| Situation | DEL | Buzzer |
|---|---|---|
| Un appareil attend (décompte, nom à choisir) | clignotement lent | 1 bip |
| **Écriture en cours : ne rien débrancher** | cœur / clignotement court | **double bip toutes les N s** (réglage, 1 s par défaut) |
| Tout est vérifié, on peut débrancher | fixe | 3 bips courts |
| Erreur (voir le téléphone) | clignotement rapide | 3 bips longs |

**Bouton :**

| Appui | Effet |
|---|---|
| Court | Démarre ce qui attend : même effet que « Tout transférer » |
| Long (3 s) | Arrêt propre ; refusé (2 bips) pendant une écriture |
| Pi éteint | Le même bouton le rallume |

**Avis franc sur le double bip à chaque seconde.** Une copie vers le tampon microSD dure près d'une heure, soit environ 3 600 double bips. Tu le couperas le deuxième soir. Je l'ai laissé à 1 s parce que tu l'as demandé, mais je te conseille 5–10 s. La DEL de façade dit déjà « occupé » en continu. L'intervalle se change dans Réglages → Témoins, et le buzzer se coupe d'une touche.

---

## 4. Boîtier fermé « au fond du sac »

Tu conçois des pièces imprimées : un boîtier maison sera meilleur que le commerce. Contraintes à respecter :

- **Chaleur.** Le Pi 4 ralentit vers 80 °C. Une copie d'une heure dans un boîtier fermé l'y amène. Prévoir un dissipateur en contact avec le SoC (pad thermique) et des fentes d'aération, ou un boîtier en aluminium passif. La bannière « sous-tension / ralenti » de l'interface le signale.
- **Guides de lumière** en face des DEL ACT/PWR, ou une DEL de façade.
- **Grille de buzzer**, et un **bouton en retrait** pour éviter les appuis dans le sac.
- **Ports.** USB-C d'alimentation, les deux USB 3 (bleus) accessibles et marqués « SSD » / « Appareil ». Avec l'option B, prévoir une baie interne et un câble court pour le SSD.
- **microSD.** Une butée ou une trappe empêche qu'elle ne sorte sous un choc (option A).
- **Câbles.** Un serre-câble ou une encoche évite qu'un câble arraché tire sur le port pendant une copie.

---

## 5. Alimentation : la cause n° 1 des copies ratées

- Le Pi 4 partage **1,2 A** entre tous ses ports USB. Un SSD plus un drone branché par câble peuvent s'en approcher : certains drones tentent de **se recharger** sur le port.
- Bloc officiel **5,1 V / 3 A**. Sur batterie externe : le Pi 4 ne négocie pas l'USB-PD, il faut une batterie qui fournit 3 A sous 5 V sans négociation.
- En cas de sous-tension, l'interface affiche une bannière. La relecture de vérification attrape les copies corrompues, mais ne remplace pas une bonne alimentation. Si le problème se répète, utiliser un **hub USB 3 alimenté** pour le SSD et l'appareil.

---

## 6. Liste d'achat

| Article | Remarque |
|---|---|
| Raspberry Pi 4 (2 Go suffisent) | déjà possédé |
| Alimentation officielle 5,1 V / 3 A USB-C | indispensable |
| microSD **High Endurance 256 Go** (option A) | OS + tampon |
| *ou* petit SSD USB 3 de 500 Go–1 To (options B/C) | tampon rapide, plus robuste |
| Lecteur de cartes USB 3 | pour les cartes retirées des appareils |
| Câbles USB-C ↔ USB-A courts et de qualité (données) | un par appareil branché en direct ; un câble « charge seulement » ne marchera pas |
| Module buzzer actif, DEL 5 mm + 330 Ω, bouton poussoir, fils Dupont | témoins, section 3 |
| Dissipateur + pad thermique | boîtier fermé |
