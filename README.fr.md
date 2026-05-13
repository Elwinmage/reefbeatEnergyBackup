# reefbeat⚡Backup

**🇫🇷 Français** · [🇬🇧 English](README.md)

---

Système autonome de monitoring et de gestion de batterie de secours pour aquarium récifal Red Sea (ReefWave, ReefRun, DC Skimmer, DC Pump).

## ⚡ Fonctionnalités

- **Monitoring batterie** via INA226 (I2C, principal) + Victron BLE (auxiliaire optionnel pour l'état du chargeur)
- **Détection de coupure instantanée** via relais 230 V sur GPIO
- **Dégradation progressive des pompes** — niveaux SoC calculés automatiquement à partir d'une cible d'autonomie
- **Contrôle individuel** — chaque ReefWave / ReefRun / Skimmer reçoit sa propre intensité par niveau
- **Failover réseau 3 niveaux** — Wi-Fi normal → reconnexion → hotspot autonome
- **Intégration Home Assistant** — auto-discovery MQTT (10 capteurs + chargeur si Victron)
- **Buffer MQTT avec replay** — les données pendant la coupure HA ne sont jamais perdues
- **Auto-détection** — scanne le réseau pour trouver les équipements ReefBeat pendant la configuration
- **Bilingue** — interface FR/EN selon la locale système

## 📋 Sommaire

- [Installation rapide](#-installation-rapide)
- [Niveaux de montage matériel](#-niveaux-de-montage-matériel)
  - [Niveau 1 — Montage de base](#niveau-1--montage-de-base)
  - [Niveau 2 — Montage normal (recommandé)](#niveau-2--montage-normal-recommandé)
  - [Niveau 3 — Montage avancé](#niveau-3--montage-avancé)
  - [Augmentation d'autonomie](#augmentation-dautonomie)
- [Configuration](#-configuration)
- [Home Assistant](#-home-assistant)
- [Blueprint test de batterie](#-blueprint-test-automatique-de-batterie)
- [Structure du projet](#-structure-du-projet)
- [Dépannage](#-dépannage)

---

## 🚀 Installation rapide

```bash
curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | sudo bash
```

L'installeur :

1. Télécharge la dernière version
2. Active l'I2C du Pi si nécessaire (`raspi-config nonint do_i2c 0`)
3. Installe `python3-rpi-lgpio` (compatible Pi 5 / kernel 6.6+) et les dépendances Python
4. Lance le wizard interactif qui :
   - Scanne le réseau pour trouver les équipements ReefBeat
   - Récupère SSID Wi-Fi et adresses MAC depuis vos équipements
   - Détecte automatiquement votre Raspberry Pi
   - Calcule les niveaux SoC à partir d'une **cible d'autonomie** (12 h, 24 h…)
   - Configure batterie, monitoring INA226 + Victron optionnel, MQTT

---

## 🔧 Niveaux de montage matériel

Le système se construit en trois niveaux, chacun ajoutant des fonctionnalités. Vous pouvez démarrer au niveau 1 et monter progressivement.

### Niveau 1 — Montage de base

> **Objectif** : assurer une alimentation des pompes sur batterie en cas de coupure secteur, sans monitoring ni automatisation.

#### 📦 Matériel

| Composant | Modèle suggéré | Prix indicatif |
|---|---|---|
| ![Batterie](docs/images/batterie.png) **Batterie LiFePO₄ 24 V 60 Ah** *(chargeur 24V/5A inclus)* | [Kepworth 24V 60Ah](https://www.amazon.fr/dp/B0F3X3LB9K) | ~260 € |
| ![Connecteur jack](docs/images/jack.png) **Connecteur jack adaptateur ReefWave** | Câble jack 5,5 × 2,1 mm vers fils nus | ~5 € |
| ![Connecteur RSRun](docs/images/rsrun.png) **Connecteur étanche IP68 4 broches ReefRun/Skimmer** | [Connecteur IP68 4 pôles](https://fr.aliexpress.com/item/1005009386771716.html) | ~5 € |
| Câblage (fil 2,5 mm² rouge/noir, cosses, gaine thermo, fusible 15 A) | — | ~20 € |

**Budget niveau 1 : ~290 €**

> 🔊 **Note bruit** : le chargeur fourni avec la batterie Kepworth est équipé d'un ventilateur de refroidissement actif relativement bruyant. Si vous comptez l'installer dans un meuble près d'une zone de vie, prévoyez un placement éloigné (cellier, cave, garage) ou envisagez le passage direct au [niveau 3](#niveau-3--montage-avancé) avec le chargeur Victron Blue Smart, beaucoup plus silencieux (ventilation passive en charge faible).

#### 🔌 Schéma de montage

```
                 230 V
                   │
                   ▼
            ┌─────────────┐
            │  Chargeur   │
            │ 24V 5A inc. │ ← fourni avec la batterie
            └──────┬──────┘
                   │  24 V DC
                   ▼
            ┌─────────────┐
            │  Batterie   │
            │  LiFePO₄    │  ← stocke l'énergie
            │  24V 60Ah   │
            └──────┬──────┘
                   │  24 V DC (avec fusible 15 A)
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
    ┌───────┐ ┌────────┐ ┌─────────┐
    │ReefRun│ │ReefWave│ │DC Skim. │
    │+pumps │ │  jack  │ │connect. │
    └───────┘ └────────┘ └─────────┘
```

#### 📝 Explications

Le principe : **la batterie est en parallèle entre le chargeur et les charges**. Elle est constamment maintenue chargée par le chargeur (fourni avec la batterie Kepworth) en mode flottant, et débite automatiquement quand le secteur tombe — il n'y a aucun commutateur, aucune électronique au milieu.

- **ReefWave** : utilise le **connecteur jack 5,5 × 2,1 mm** (positif au centre)
- **ReefRun et DC Skimmer** : utilisent le **connecteur étanche IP68 4 broches** propriétaire Red Sea (la pompe inclut son propre régulateur, le 24 V brut suffit)
- Le chargeur fourni reste branché en permanence : il bascule automatiquement en mode flottant une fois la pleine charge atteinte

> ⚠️ **Sécurité** : un **fusible 15 A** sur le pôle + de la batterie, juste après celle-ci, est obligatoire. Ce calibre est calé sur la capacité du câble 2,5 mm² (~16 A maximum) et offre une marge confortable face à une consommation pic typique de ~9 A (2× ReefWave 45 + ReefRun 12000 + Skimmer + Pi). En cas de court-circuit côté charges, c'est ce qui sauve la batterie (et la maison).

#### ✅ Ce que vous obtenez

- Continuité électrique pendant les coupures (autonomie ~6-12 h selon vos pompes)
- Aucune intervention nécessaire à la coupure
- Aucun monitoring, aucune dégradation : les pompes tournent à 100 % jusqu'à ce que la batterie soit vide

#### ❌ Limitations

- Aucune visibilité sur l'état de la batterie
- Aucune gestion de la dégradation : la batterie se vide vite, tout s'éteint d'un coup à la fin
- Risque de décharge profonde répétée → vieillissement accéléré

---

### Niveau 2 — Montage normal *(recommandé)*

> **Objectif** : ajouter le monitoring batterie temps réel, la détection automatique de coupure, et la dégradation progressive des pompes selon le SoC. C'est le niveau **recommandé** pour une installation pérenne.

#### 📦 Matériel additionnel (en plus du niveau 1)

| Composant | Modèle suggéré | Prix indicatif |
|---|---|---|
| ![INA226](docs/images/ina226.png) **Module INA226 0-36V/20A** (shunt 2 mΩ embarqué) | [Fasizi INA226 20A](https://www.amazon.fr/dp/B0B7MYYT2V) | ~14 € |
| ![Pi](docs/images/rpi.png) **Raspberry Pi 3 B+** (ou plus récent) | [Pi 3 B+ 1 Go chez Kubii](https://www.kubii.com/fr/cartes-nano-ordinateurs/2119-raspberry-pi-3-modele-b-1-gb-kubii-5056561800318.html) | ~40 € |
| Carte microSD 16 Go classe 10 + alim USB du Pi | — | ~15 € |
| Convertisseur DC-DC 24 V → 5 V 3 A pour le Pi | Step-down buck regulator | ~8 € |
| ![Finder](docs/images/finder.png) **Relais Finder 40.61.8.230.4000** (bobine 230 V, 1 NO/NC) | [Finder 40.61](https://www.amazon.fr/dp/B003A611AE) | ~12 € |
| ![Support Finder](docs/images/support.png) **Socle DIN Finder 95.95.3** | [Finder 95.95.3](https://www.amazon.fr/dp/B0018L99AC) | ~8 € |
| Rail DIN 35 mm (10 cm) + petit boîtier électrique | — | ~15 € |

**Budget additionnel : ~112 €** — **Budget cumulé niveau 2 : ~402 €**

#### 🔌 Schéma de montage

```
                 230 V ─────┬───────────────┐
                            │               │
                            ▼               ▼
                     ┌─────────────┐   ┌──────────┐
                     │  Chargeur   │   │  Relais  │
                     │ Victron 24V │   │  Finder  │
                     └──────┬──────┘   │   40.61  │
                            │ 24V      │  bobine  │
                            ▼          │   230V   │
                     ┌─────────────┐   └────┬─────┘
                     │  Batterie   │        │ NO/NC
              ┌──────┤  LiFePO₄    │        │ contact
              │      └──────┬──────┘        │
              │             │ 24V           │
              │      [Shunt INA226]         │
              │             │               │
              │             ▼               │
              │    ┌────────────────┐       │
              │    │ DC-DC 24V→5V   │       │
              │    └────────┬───────┘       │
              │             │ 5V            │
              │             ▼               │
              │    ┌────────────────┐       │
              ├────│  Raspberry Pi  │◄──────┘
              │I2C │   GPIO 26      │ GPIO state
              │SDA │   GPIO 2 SDA   │
              │SCL │   GPIO 3 SCL   │
              │    └────────────────┘
              │
              ▼
       ReefRun / ReefWave / DC Skimmer
```

#### 📝 Explications

**Câblage du shunt INA226** (le plus important) :

Le module INA226 doit être **en série sur le pôle + de la batterie**, entre la batterie et toutes les charges. C'est ce qui lui permet de mesurer le courant net entrant/sortant.

```
Batterie (+) ──► [IN+ shunt INA226 IN−] ──► Bus + 24V ─┬─► Chargeur (sortie)
                                                        ├─► DC-DC vers Pi
                                                        ├─► ReefRun
                                                        ├─► ReefWave
                                                        └─► DC Skimmer

Batterie (−) ──────────────────────────► Bus − (commun)
```

Le shunt voit donc :
- **courant positif** = la batterie débite (décharge ou alimentation des charges)
- **courant négatif** = la batterie reçoit (charge depuis le Victron)

**Câblage du relais de détection de coupure** :

Le relais Finder 40.61.8.230 est un **détecteur d'absence de tension secteur** : sa bobine est alimentée en 230 V, ses contacts NO/NC basculent quand le secteur tombe.

| Borne du socle 95.95.3 | Connexion |
|---|---|
| A1 | Phase 230 V |
| A2 | Neutre 230 V |
| 11 (commun) | GND du Pi |
| 12 (NC) | GPIO 26 du Pi (avec pull-up interne) |

Sur secteur OK, la bobine est alimentée → contact NC ouvert → GPIO lit 1 (tiré vers 3.3 V par pull-up).
Sur coupure, la bobine retombe → contact NC fermé → GPIO tiré à GND, lit 0.

**Connexions Pi → INA226** (4 fils) :

| Pi GPIO | INA226 |
|---|---|
| Pin 1 (3.3 V) | VCC |
| Pin 6 (GND) | GND |
| Pin 3 (GPIO 2 SDA) | SDA |
| Pin 5 (GPIO 3 SCL) | SCL |

#### ✅ Ce que vous obtenez

- **Monitoring temps réel** : tension batterie, courant, puissance, SoC calculé en coulomb counting
- **Détection de coupure en < 1 seconde** via le relais
- **Dégradation automatique** : les ReefWave passent à 70 %, puis 50 %, puis 10 % au fil du SoC qui baisse ; le skimmer s'arrête en mode survie ; etc.
- **Snapshots de configuration** : à la coupure, la conf d'origine de chaque pompe est sauvegardée sur disque ; au retour, elle est restaurée à l'identique
- **Buffer MQTT** : pendant que HA est down (ce qui arrive presque toujours pendant une vraie coupure), les mesures sont stockées localement et rejouées dès que le broker remonte → vous avez la **courbe de décharge complète** dans HA
- **Failover réseau** : si la box Wi-Fi tombe aussi, le Pi bascule en hotspot pour rester joignable

---

### Niveau 3 — Montage avancé

> **Objectif** : ajouter le contrôle à distance du chargeur, un disjoncteur connecté pour pouvoir déclencher des **tests de décharge programmés** depuis Home Assistant, et un modem 4G pour les notifications même quand tout le réseau est coupé.
>
> Les trois ajouts de ce niveau sont **indépendants** — vous pouvez installer la combinaison de votre choix :

| Ajout | But | Installable seul ? |
|---|---|---|
| 🔌 **Chargeur Victron BLE** | Chargeur silencieux + état chargeur dans HA | ✅ Oui |
| ⚡ **Disjoncteur connecté** | Tests de décharge automatisés depuis HA | ✅ Oui |
| 📶 **Modem USB 4G LTE** | Notifications même quand le Wi-Fi est coupé | ✅ Oui |

#### 📦 Matériel additionnel (en plus du niveau 2)

| Composant | Modèle suggéré | Prix indicatif |
|---|---|---|
| ![Chargeur BLE](docs/images/chargeur.png) **Chargeur Victron Blue Smart IP22 24/12** *(remplace le chargeur Kepworth fourni — silencieux + BLE)* | [Victron Blue Smart IP22 24/12](https://www.amazon.fr/dp/B08P4Z8NL6) | ~155 € |
| ![Disjoncteur](docs/images/disjoncteur.png) **Disjoncteur connecté Wi-Fi 16 A avec compteur** | [Tongou TO-Q-SY1-JWT](https://www.amazon.fr/dp/B08ND2RGX8) | ~30 € |
| ![Huawei E3372h](docs/images/huawei-e3372h-320.png) **Modem USB 4G LTE Huawei E3372h-320** | [Huawei E3372h-320](https://www.amazon.fr/HUAWEI-51071SMK-Huawei-E3372h-320-LTE-Stick/dp/B085RDTZMP) | ~40 € |

**Budget additionnel maximal : ~225 €** (les trois) — **Budget cumulé niveau 3 : ~627 €**

#### 🔌 Schéma de montage

```
        230 V ──► [Disjoncteur Tongou Wi-Fi] ──┬──────────────┐
                                                │              │
                                                ▼              ▼
                                         ┌─────────────┐   ┌──────────┐
                                         │ Chargeur    │   │  Relais  │
                                         │Victron BLE  │   │  Finder  │
                                         │24/12 Smart  │   │ détection│
                                         └──────┬──────┘   └────┬─────┘
                                                │ 24 V           │
                                                ▼                │
                                         ┌─────────────┐         │
                                         │  Batterie   │◄────[shunt INA226]
                                         └──────┬──────┘         │
                                                │ 24 V           │
                                                ▼                │
                                          (charges)              │
                                                                 │
                                          ┌──── Wi-Fi ───┐       │
                                          │              │       │
                                          ▼              ▼       │
                                    Home Assistant   Raspberry Pi
                                    (intégration         GPIO 26 ◄┘
                                     Tongou)              │
                                          │            USB │
                                          │ BLE           ▼
                                          ▼         ┌───────────┐
                                    Chargeur Victron│  Huawei   │
                                    (état temps réel│ E3372h    │
                                                   │  4G LTE   │
                                                   └───────────┘
```

#### 📝 Explications

**Disjoncteur Tongou TO-Q-SY1-JWT** :

Disjoncteur DIN modulaire qui se commande via Wi-Fi (protocole Tuya, intégrable à HA via [Local Tuya](https://github.com/rospogrigio/localtuya) ou l'intégration Tuya Cloud officielle). Il fournit aussi la mesure consommation en kWh / V / A en temps réel — utile pour vérifier que le chargeur bascule bien sur batterie quand on simule une coupure.

**Câblage** : le disjoncteur s'installe **juste avant** le chargeur Victron et le relais Finder. Quand on le coupe via HA, c'est exactement comme une vraie coupure secteur :

- Le chargeur ne fournit plus rien
- Le relais Finder voit l'absence de tension → bascule de contact
- Le Pi voit la coupure via GPIO et déclenche immédiatement la dégradation

**Chargeur Victron Blue Smart IP22 24/12 (avec BLE)** :

Remplace le chargeur Kepworth fourni avec la batterie. Outre l'ajout du Bluetooth Low Energy, **il est nettement plus silencieux** : ventilation passive en charge faible, le ventilateur ne se déclenche qu'en pleine charge à plus de 8 A. Idéal si le système est installé dans une pièce de vie.

Permet de remonter dans HA :
- L'état du chargeur (`storage` / `bulk` / `absorption` / `float`)
- La tension et le courant de sortie temps réel
- Les éventuelles erreurs (overheat, battery voltage out of range…)

Configuration : récupérer la **clé de chiffrement** depuis l'app VictronConnect (Settings → Product Info → Instant Readout → "Show"), à entrer dans le wizard de configuration.

**Modem USB 4G LTE Huawei E3372h-320** :

<p align="center">
  <img src="docs/images/huawei-e3372h-320.png" alt="Huawei E3372h-320" width="300">
</p>

LTE Cat4 150 Mbps, bandes 1/3/7/8/20 (800/900/1800/2100/2600 MHz), mode HiLink plug-and-play. Il suffit de le brancher sur un port USB du Pi avec une carte SIM active — il crée une interface Ethernet virtuelle (`eth1`), aucun pilote ni configuration PPP nécessaire.

Quand le Wi-Fi et le routeur sont tous les deux down, le notifier détecte automatiquement le modem, vérifie la connectivité cellulaire, et route les notifications ntfy.sh à travers la 4G. Interface web HiLink accessible sur `http://192.168.8.1` pour le monitoring signal/état.

**Passerelle internet 4G pour les ReefBeat** : quand le hotspot RPi est actif et cette option activée, le RPi fait office de routeur NAT — il redirige le trafic internet des ReefBeat (connectés au hotspot) à travers le modem 4G. Résultat : **l'app mobile Red Sea continue de fonctionner** pendant une coupure, car les contrôleurs ReefBeat accèdent toujours aux serveurs cloud Red Sea.

#### ✅ Ce que vous obtenez

- **Contrôle distant du secteur** vers la batterie depuis HA
- **Tests de décharge programmés** : voir la [section blueprint](#-blueprint-test-automatique-de-batterie)
- **Visibilité complète** sur le chargeur (mode, courant, erreurs)
- **Mesure de la consommation totale** en kWh via le disjoncteur Tongou (utile pour le calcul d'autonomie réelle)
- **Notifications même quand tout est coupé** via 4G LTE
- **L'app mobile Red Sea continue de fonctionner** pendant les coupures (la passerelle 4G route le trafic ReefBeat vers le cloud)

---

### Augmentation d'autonomie

> **Objectif** : doubler (ou plus) la capacité batterie pour des coupures plus longues.

Le moyen le plus simple et le plus sûr est d'ajouter une ou plusieurs **batteries identiques en parallèle**. Les batteries LiFePO₄ avec BMS interne (comme la Kepworth 24V 60Ah) acceptent ce mode de fonctionnement nativement.

#### 📦 Matériel par batterie additionnelle

| Composant | Prix indicatif |
|---|---|
| 1× batterie LiFePO₄ 24V 60Ah identique | ~260 € |
| 2× câbles de liaison 2,5 mm² (50 cm rouge + 50 cm noir, cosses serties) | ~10 € |
| 1× fusible **inline 15 A** (un par batterie additionnelle) | ~3 € |

**Budget par +60 Ah : ~273 €**

#### 🔌 Schéma de montage parallèle

```
                Bus + (vers chargeur et charges)
                      ▲
                      │
        ┌─────────────┼─────────────┐
        │             │             │
   [fusible]     [fusible]      [fusible]
   15 A          15 A           15 A
        │             │             │
   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐
   │ Bat #1  │   │ Bat #2  │   │ Bat #3  │
   │24V 60Ah │   │24V 60Ah │   │24V 60Ah │
   └────┬────┘   └────┬────┘   └────┬────┘
        │             │             │
        └─────────────┼─────────────┘
                      │
                      ▼
                Bus − (commun)
```

#### 📝 Règles importantes

1. **Batteries identiques uniquement** : même marque, même modèle, idéalement même âge. Mélanger des batteries de capacités ou d'âges différents fait travailler la plus faible en surcharge → vieillissement accéléré.
2. **Équilibrage initial** : avant la mise en parallèle, charger chaque batterie individuellement à 100 % et vérifier qu'elles sont à la même tension (±0,1 V). Sinon, l'équilibrage se fera par un courant fort entre batteries au branchement → risque de fusion des cosses.
3. **Câbles de liaison de section égale** : si une batterie a un câble plus long ou plus fin que les autres, elle débitera moins → déséquilibre permanent.
4. **Un fusible par batterie**, pas un seul fusible commun : en cas de défaut sur une batterie, seule celle-là est isolée.
5. **Pas de modification du shunt INA226** : il reste sur le bus commun, il voit alors le courant **total** des deux batteries cumulées — c'est exactement ce qu'on veut pour le SoC.

#### 📊 Capacités cumulées et autonomies estimées

Pour un setup typique (2× ReefWave 45 + 1× ReefRun 12000 + DC Skimmer + Pi) :

| Configuration | Capacité utile | Autonomie cible 24 h |
|---|---|---|
| 1× 60 Ah | 1228 Wh | atteignable (estimé 32 h) |
| 2× 60 Ah | 2457 Wh | confortable (estimé 60 h+) |
| 3× 60 Ah | 3686 Wh | luxueuse (90 h+) |

> ⚠️ Re-lancez le wizard `configure.py` après ajout d'une batterie pour mettre à jour la capacité totale dans `config.json`. Le calcul de scénario en tiendra compte automatiquement.

---

## ⚙️ Configuration

Le wizard `configure.py` est interactif et bilingue (FR/EN selon la locale). Il guide à travers 6 étapes :

1. **Réseau** — confirmation du SSID Wi-Fi domestique (lu depuis NetworkManager)
2. **Détection des équipements ReefBeat** — scan automatique du sous-réseau, sélection des équipements à mettre sur batterie
3. **Détection de coupure** — choix entre relais GPIO (recommandé) et monitoring de courant
4. **Batterie** — capacité (Ah) du pack
5. **Monitoring** — INA226 (obligatoire, détection auto sur I2C) + Victron BLE (optionnel)
6. **Mode de secours** — choix entre :
   - **Auto** (recommandé) : on donne une cible d'autonomie, le wizard détecte le Pi, demande les charges auxiliaires, et calcule les niveaux SoC + intensités optimales
   - **Simple** : une seule vitesse de secours sur tout

Le résultat est sauvegardé dans `config.json` et peut être édité à la main si besoin.

---

## 🏠 Home Assistant

### Capteurs auto-publiés

Tous les capteurs apparaissent automatiquement dans HA après publication des configs MQTT discovery.

| Capteur | Description |
|---|---|
| `sensor.reef_battery_voltage` | Tension batterie (V) |
| `sensor.reef_battery_current` | Courant (A, + = décharge) |
| `sensor.reef_battery_power` | Puissance (W) |
| `sensor.reef_battery_soc` | State of Charge (%) |
| `sensor.reef_battery_power_state` | mains / battery |
| `sensor.reef_battery_pump_intensity` | Intensité pompes moyenne (%) |
| `sensor.reef_battery_runtime` | Autonomie estimée (h) |
| `sensor.reef_battery_outage_duration` | Durée coupure courante (min) |
| `sensor.reef_battery_network_mode` | client / rejoin / hotspot |
| `sensor.reef_battery_monitor_source` | ina226 |

**Si Victron BLE est configuré** (niveau 3) :

| Capteur | Description |
|---|---|
| `sensor.reef_battery_charger_voltage` | Tension de sortie chargeur (V) |
| `sensor.reef_battery_charger_current` | Courant de sortie chargeur (A) |
| `sensor.reef_battery_charger_state` | bulk / absorption / float / storage |
| `sensor.reef_battery_charger_error` | no_error / … |

### Buffer MQTT

Pendant une coupure, HA et le broker MQTT sont presque toujours indisponibles (ils sont sur la même infra que le secteur). Le service écrit toutes les mesures dans `/var/lib/reef-battery-monitor/mqtt/messages.jsonl` et les rejoue automatiquement dès que le broker remonte → vous obtenez la courbe complète a posteriori, sans trou.

Configuration optionnelle dans `config.json` :

```json
"mqtt": {
  "buffer_dir": "/var/lib/reef-battery-monitor/mqtt",
  "buffer_retention_days": 7
}
```

---

## 🤖 Blueprint test automatique de batterie

> **Disponible uniquement avec le niveau 3** (disjoncteur Tongou requis).

Ce blueprint Home Assistant déclenche périodiquement un **test de décharge réel** : il coupe le disjoncteur secteur pendant 40 minutes, observe la courbe de décharge, et la compare au prévisionnel calculé par le scénario.

### Principe

```
Date programmée (ex: dernier dimanche du mois, tous les 3 mois)
      │
      ▼
Présence "user_y" détectée à la maison ?
      │
      ├─── Non ──► Test annulé silencieusement
      │
      └─── Oui
              │
              ▼
        Notif HA actionnable sur téléphone
        "Lancer test batterie 40 min ?"
        (pas de timeout : attend une réponse explicite)
              │
              ├─── Refus ──────────────────► Annulé
              │
              └─── Accept
                      │
                      ▼
              Disjoncteur OFF
              SoC / tension / puissance initiaux sauvegardés
              Calcul du forecast (puissance × durée / capacité)
                      │
                      ▼
              Attendre 40 min, OU abort immédiat si tension < seuil
              (le service bascule en mode batterie,
               le buffer MQTT enregistre tout)
                      │
                      ▼
              Disjoncteur ON
                      │
                      ▼
              Analyse 3 axes :
                📊 Forecast : SoC consommé réel vs prévision
                🔋 Profil tension : tension finale dans le plateau LFP ?
                ⏱  Autonomie extrapolée jusqu'à 20% SoC
                      │
                      ▼
              Notif récap au mobile + log HA
```

### Installation du blueprint

Le blueprint est fourni dans le dépôt sous [`blueprints/reef_battery_test.yaml`](blueprints/reef_battery_test.yaml).

Pour l'installer dans HA :

1. Copier le fichier vers `<config>/blueprints/automation/reefbeat/reef_battery_test.yaml`
2. Recharger les blueprints dans HA (Paramètres → Automatisations → ⋮ → Recharger)
3. Créer une nouvelle automatisation à partir de ce blueprint
4. Renseigner :
   - **Heure** (ex. 14:00) — éviter les heures de nourrissage
   - **Jour de la semaine** : lundi à dimanche
   - **Occurrence** : 1er, 2ème, 3ème, 4ème, ou **dernier** (recommandé pour les week-ends)
   - **Période** (mois) : 1, 3, 6 mois entre tests
   - **Personne dont la présence est requise** : ex. `person.elwin`
   - **Service de notification** : ex. `mobile_app_pixel_8` (sans le `notify.` préfixe)
   - **Disjoncteur connecté** : entité switch du Tongou
   - **Capteur SoC / tension / puissance** : `sensor.reef_battery_*`
   - **Capacité batterie** (Wh) : ex. 1228 pour 60Ah × 25.6V × 0.8 DoD
   - **Durée du test** (min) : 40 par défaut
   - **Tolérance écart forecast** (% SoC) : 3 par défaut
   - **Seuil tension d'arrêt d'urgence** (V) : 24.0 par défaut
   - **Plateau LFP minimum** (V) : 25.6 par défaut

### Précautions importantes

⚠️ **Ne jamais lancer un test sans personne à la maison** : si la batterie est en mauvais état ou si le scénario est mal calibré, le test peut entraîner l'arrêt total des pompes après les 40 minutes. Un humain doit pouvoir intervenir manuellement.

⚠️ **Première utilisation** : faire un test **manuel** d'abord (couper le disjoncteur à la main pendant 5-10 min) pour vérifier que tout le système réagit correctement avant de faire des tests automatisés de 40 min.

⚠️ **Timing** : éviter les heures de nourrissage des poissons / coraux. Choisir un créneau calme.

---

## 📁 Structure du projet

```
install.sh                          Installeur (curl | bash)
configure.py                        Wizard interactif
config.example.json                 Template par défaut
config.json                         Votre configuration (généré par le wizard)
main.py                             Boucle principale du service
monitor.py                          Backend INA226 + auxiliaire Victron BLE
outage.py                           Détection de coupure (relais GPIO)
hotspot.py                          Failover réseau 3 niveaux
controller.py                       Contrôle pompes + orchestration coupure
mqtt_buffer.py                      Buffer MQTT avec replay
power_estimation.py                 Tables de conso + builder de scénario
ble_scan.py                         Scanner BLE Victron (utilisé par le wizard)
setup.py                            Installeur de dépendances
reef-battery-monitor.service        Unité systemd
docs/
  images/                           Images des composants pour la doc
blueprints/
  reef_battery_test.yaml            Blueprint HA de test de batterie
```

---

## 🐛 Dépannage

Voir [TROUBLESHOOTING.md](TROUBLESHOOTING.md) pour les problèmes courants :

- `Failed to add edge detection` → installer `python3-rpi-lgpio`
- INA226 lit `0.000A` → vérifier le câblage en série du shunt
- Victron `'Scanner' has no attribute 'scan'` → version `victron-ble` incompatible
- MQTT discovery sensors absents → vérifier les credentials et le `base_topic`

---

## 📜 Licence

MIT

## 🔗 Projets liés

- [ha-reefbeat-component](https://github.com/Elwinmage/ha-reefbeat-component) — Intégration Home Assistant pour les équipements Red Sea ReefBeat
- [ha-reef-card](https://github.com/Elwinmage/ha-reef-card) — Carte Lovelace HA pour la gestion d'aquarium
