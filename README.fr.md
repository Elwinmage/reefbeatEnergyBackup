# reefbeat⚡Backup

**🇫🇷 Français** · [🇬🇧 English](README.md)

---

Système autonome de monitoring et de gestion de batterie de secours pour aquarium récifal Red Sea (ReefWave, ReefRun, DC Skimmer, DC Pump).

## ⚡ Fonctionnalités

- **Monitoring batterie** via INA226 (I2C, principal) + Victron BLE (auxiliaire optionnel pour l'état du chargeur)
- **Détection de coupure instantanée** via relais 230 V sur GPIO
- **Dégradation progressive des pompes** — niveaux SoC calculés automatiquement à partir d'une cible d'autonomie
- **Contrôle individuel** — chaque ReefWave / ReefRun / Skimmer reçoit sa propre intensité par niveau
- **Failover réseau 3 niveaux** — Wi-Fi normal → reconnexion → hotspot autonome avec liste blanche MAC, réservations DHCP et remappage d'IP automatique des pompes
- **Récupération robuste** — snapshots de config pompes (pré-coupure + référence horaire), restauration avec retry et CLI manuelle, health-check périodique
- **Intégration Home Assistant** — auto-discovery MQTT (capteurs + chargeur si Victron + entités de test)
- **Buffer MQTT avec replay** — les données pendant la coupure HA ne sont jamais perdues
- **Auto-détection** — scanne le réseau pour trouver les équipements ReefBeat pendant la configuration, relève leurs MAC
- **Test intégré** — palier de test pour vérifier le pilotage des pompes (vitesse + on/off) et coupure Wi-Fi temporisée, sans attendre une vraie coupure
- **Mise à jour automatique** — vérifie GitHub pour les nouvelles versions, entité `update` dans HA avec bouton "Installer"
- **Redémarrage programmé** — reboot automatique du RPi via cron, annulé si sur batterie
- **Bilingue** — interface FR/EN selon la locale système

## 📋 Sommaire

- [Installation rapide](#-installation-rapide)
- [Niveaux de montage matériel](#-niveaux-de-montage-matériel)
  - [Niveau 1 — Montage de base](#niveau-1--montage-de-base)
  - [Niveau 2 — Montage normal (recommandé)](#niveau-2--montage-normal-recommandé)
  - [Niveau 3 — Montage avancé](#niveau-3--montage-avancé)
  - [Augmentation d'autonomie](#augmentation-dautonomie)
- [Configuration](#-configuration)
- [Failover réseau](#-failover-réseau--flux-complet)
- [Fiabilité & récupération](#-fiabilité--récupération)
- [Home Assistant](#-home-assistant)
- [Blueprint test de batterie](#-blueprint-test-automatique-de-batterie)
- [Structure du projet](#-structure-du-projet)
- [ReefWave et synchronisation cloud](#-important--reefwave-et-synchronisation-cloud)
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

#### 🔌 Guide de fabrication des câbles

##### ReefWave — Jack 5,5 × 2,1 mm

<p align="center">
  <img src="docs/images/jack-polarity.png" alt="Polarité jack +24V au centre" width="200">
</p>

| Broche | Connexion |
|--------|-----------|
| **Broche centrale** (intérieur) | **+24V** |
| **Manchon extérieur** | **GND (−)** |

Polarité standard positif au centre. Soudez ou sertissez un fil rouge 2,5 mm² sur la broche centrale et un fil noir sur le manchon.

##### ReefRun / DC Skimmer — Connecteur IP68 4 broches

<p align="center">
  <img src="docs/images/rsrun-pinout.png" alt="Brochage connecteur IP68 4 broches" width="300">
</p>

| Broche | Couleur | Connexion |
|--------|---------|-----------|
| **1** (rouge) | Rouge | **+24V** |
| **2** (rouge) | Rouge | **+24V** |
| **3** (blanc) | Noir | **GND (−)** |
| **4** (blanc) | Noir | **GND (−)** |

> ⚠️ **Distinction importante :**
>
> - **DC Skimmer** (moteur unique) : il suffit de câbler les **broches 1 et 3** (+24V et GND). Les broches 2 et 4 peuvent rester non connectées.
> - **Pompe de remontée (ReefRun)** : il faut **câbler les 4 broches** — broches 1+2 pour le +24V, broches 3+4 pour le GND. La pompe de remontée consomme plus de courant et utilise les deux paires de broches pour répartir la charge. Câbler seulement 2 broches risque de faire surchauffer le connecteur.

> 🔴 **CRITIQUE — vérifiez au multimètre avant le branchement sur la batterie !**
>
> 1. Réglez votre multimètre en mode **tension continue (DC V)**
> 2. Touchez les sondes sur les broches 1 (+) et 3 (−) de votre câble assemblé
> 3. Connectez brièvement à la batterie
> 4. Vérifiez que vous lisez **+24V à +28V** (pas négatif !)
> 5. Une inversion de polarité **détruira** le contrôleur ReefBeat instantanément
>
> **Vérifiez chaque câble avant la première utilisation. Il n'y a pas de deuxième chance.**

> ⚠️ **Sécurité** : un **fusible 15 A** sur le pôle + de la batterie, juste après celle-ci, est obligatoire. Ce calibre est calé sur la capacité du câble 2,5 mm² (~16 A maximum) et offre une marge confortable face à une consommation pic typique de ~9 A (2× ReefWave 45 + ReefRun 12000 + Skimmer + Pi). En cas de court-circuit côté charges, c'est ce qui sauve la batterie (et la maison).

> 🔧 **À FAIRE après le passage sur batterie — recalibrer les sondes de l'écumeur**
>
> La tension d'alimentation sur batterie LiFePO4 24 V (≈ 24 à 28 V selon le SoC) **n'est pas la même** que celle du transformateur Red Sea d'origine. Comme la vitesse du moteur du skimmer dépend directement de la tension, le débit d'air et le niveau d'eau dans le godet changent une fois branché sur batterie.
>
> **Après le branchement sur batterie, recalibrez les sondes / le réglage de l'écumeur** (point de fonctionnement et niveau de débordement) sur la nouvelle tension, sinon le godet peut déborder ou l'écumage devenir inefficace. Refaites la calibration à l'alimentation effectivement utilisée en fonctionnement normal (batterie en floating ou transfo, selon votre montage).

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
| Carte microSD 16 Go classe 10 + câble USB pour le Pi | — | ~15 € |
| ![Finder](docs/images/finder.png) **Relais Finder 40.61.8.230.4000** (bobine 230 V, 1 NO/NC) | [Finder 40.61](https://www.amazon.fr/dp/B003A611AE) | ~12 € |
| ![Support Finder](docs/images/support.png) **Socle DIN Finder 95.95.3** | [Finder 95.95.3](https://www.amazon.fr/dp/B0018L99AC) | ~8 € |
| Rail DIN 35 mm (10 cm) + petit boîtier électrique | — | ~15 € |

**Budget additionnel : ~104 €** — **Budget cumulé niveau 2 : ~394 €**

> 💡 **Alimentation du Pi** : la batterie Kepworth 24V 60Ah dispose d'un **port USB 5V intégré**, qui alimente directement le Raspberry Pi. Aucun convertisseur DC-DC 24V→5V n'est nécessaire — il suffit d'un câble USB entre le port 5V de la batterie et le Pi.

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
              │      │ (port 5V    │        │
              │      │  intégré)   │        │
              │      └──┬───────┬──┘        │
              │         │ 24V   │ 5V (USB)  │
              │  [Shunt INA226] │           │
              │         │       ▼           │
              │         │  ┌────────────────┐
              ├─────────┘  │  Raspberry Pi  │◄──────┘
              │       I2C  │   GPIO 26      │ GPIO state
              │       SDA  │   GPIO 2 SDA   │
              │       SCL  │   GPIO 3 SCL   │
              │            └────────────────┘
              │
              ▼
       ReefRun / ReefWave / DC Skimmer
```

#### 📝 Explications

**Câblage du shunt INA226** (le plus important) :

Le module INA226 doit être **en série sur le pôle + de la batterie**, entre la batterie et toutes les charges. C'est ce qui lui permet de mesurer le courant net entrant/sortant.

```
Batterie (+) ──► [IN+ shunt INA226 IN−] ──► Bus + 24V ─┬─► Chargeur (sortie)
                                                        ├─► ReefRun
                                                        ├─► ReefWave
                                                        └─► DC Skimmer

Batterie (−) ──────────────────────────► Bus − (commun)

(Le Raspberry Pi est alimenté séparément par le port USB 5V intégré de la
 batterie, en aval du shunt — voir la note sur le coulomb counting ci-dessous.)
```

Le shunt voit donc :
- **courant positif** = la batterie débite (décharge ou alimentation des charges)
- **courant négatif** = la batterie reçoit (charge depuis le Victron)

> ⚠️ **Conso du Pi non mesurée** : comme le Raspberry Pi est alimenté par le port USB 5V intégré de la batterie (en aval du shunt INA226), sa consommation (~0,5–1 A en 5V, soit ~0,1–0,2 A ramené au 24V) **n'est pas comptabilisée** par le coulomb counting. L'autonomie réelle sera donc légèrement inférieure à l'estimation. Si vous voulez une mesure exacte, alimentez le Pi via un convertisseur DC-DC 24V→5V branché **en aval du shunt** (sur le bus 24V) plutôt que sur le port 5V de la batterie.

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

> 💡 **Alternative au modem USB** : vous pouvez utiliser un smartphone branché en USB (tethering) à la place de l'E3372h. Voir la section [tethering](#alternative-tethering-usb) ci-dessous.

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

> 🔑 **Code PIN SIM** : pendant la configuration, le wizard détecte si la SIM demande un PIN, le saisit, et propose de **le désactiver définitivement**. C'est fortement recommandé — sans ça, le modem ne peut pas se reconnecter automatiquement après un cycle d'alimentation.

**Passerelle internet 4G pour les ReefBeat** : quand le hotspot RPi est actif et cette option activée, le RPi fait office de routeur NAT — il redirige le trafic internet des ReefBeat (connectés au hotspot) à travers le modem 4G. Résultat : **l'app mobile Red Sea continue de fonctionner** pendant une coupure, car les contrôleurs ReefBeat accèdent toujours aux serveurs cloud Red Sea.

##### Alternative tethering USB

Si vous ne souhaitez pas acheter un modem USB, vous pouvez utiliser un **smartphone branché en USB** comme modem 4G/5G. Activez le partage de connexion USB sur le téléphone (Paramètres → Réseau → Point d'accès → Partage USB), branchez-le au RPi, et le wizard le détectera.

**Avantages** : pas de matériel supplémentaire, utilise votre téléphone et forfait existants. Le téléphone est alimenté via USB par le RPi (qui est sur batterie), il reste donc chargé pendant la coupure.

**Inconvénients** : le partage USB peut devoir être réactivé après un redémarrage du téléphone, et le téléphone doit rester physiquement connecté. L'E3372h est totalement autonome et toujours prêt.

> ⚠️ **Limitation importante — perte du tethering au reboot du RPi** : la plupart des smartphones (Android comme iPhone) **désactivent automatiquement le partage de connexion USB dès que le lien USB est coupé ou réinitialisé**. Or un redémarrage du RPi réinitialise le bus USB : au retour, le téléphone a coupé le partage et l'interface `usb0` ne réapparaît pas. C'est un comportement volontaire du système d'exploitation du téléphone, pas un bug du projet — et il ne peut pas être contourné depuis le RPi.
>
> Pour diagnostiquer après un reboot (sans toucher au téléphone) :
> ```bash
> ip link show        # usb0 apparaît-il ?
> ip addr show usb0   # a-t-il une IP ?
> ```
> - `usb0` **absent** → le téléphone a coupé le partage. Seules solutions : réactiver manuellement le partage USB sur le téléphone, utiliser une app de réactivation automatique (Tasker / « USB Tether Auto » sur Android, fiabilité variable selon le modèle), ou passer sur la clé E3372h.
> - `usb0` **présent mais sans IP** → le téléphone tient le partage mais le RPi n'a pas reconfiguré l'interface ; ce cas se règle côté RPi (relance de `dhcpcd`/`dhclient` sur `usb0`).
>
> 🛠️ **Solution Android — réactivation automatique du tethering (cas `usb0` absent)**
>
> La plupart des Android permettent de forcer le **partage de connexion USB** comme mode USB par défaut, via les **options pour développeurs**. Ainsi, le téléphone réactive automatiquement le tethering dès que le RPi se reconnecte (après un reboot, par exemple), sans intervention manuelle :
>
> 1. **Activer les options pour développeurs** : *Paramètres → À propos du téléphone* → tapez **7 fois** sur **Numéro de build** (un message « Vous êtes maintenant développeur » apparaît).
> 2. Ouvrez *Paramètres → Système → Options pour développeurs* (l'emplacement exact varie selon le constructeur).
> 3. Cherchez **Configuration USB par défaut** et sélectionnez **Partage de connexion par USB** (selon la surcouche : « USB tethering », « Connexion USB », etc.).
> 4. Optionnel : si présent, activez aussi **Accélération matérielle du tethering**.
> 5. Laissez le téléphone branché au RPi. Au prochain reboot du RPi, le tethering doit revenir tout seul.
>
> ⚠️ Le libellé et l'emplacement de ces réglages **varient selon la version d'Android et le constructeur** (Samsung/One UI, Pixel, Xiaomi…). Sur certaines surcouches, le réglage ne « tient » pas parfaitement après une mise à jour système — testez en faisant un vrai reboot du RPi et en vérifiant avec `ip addr show usb0`. Si ça ne tient pas sur votre modèle, la clé E3372h reste la solution la plus sûre.
>
> 🔑 **Recommandation** : pour un secours **fiable et non surveillé**, préférez la clé **Huawei E3372h**. En mode HiLink, elle est totalement autonome, se réinitialise seule et survit sans problème aux redémarrages du RPi. Le tethering smartphone reste une solution de dépannage acceptable uniquement si vous pouvez réactiver le partage manuellement, ou si le RPi ne redémarre jamais sans surveillance.

> ℹ️ **Priorité de routage automatique** : lors de la configuration, le wizard force une **métrique haute (700)** sur l'interface tethering (`usb0`) dans `/etc/dhcpcd.conf`. Sans ça, dhcpcd peut donner à `usb0` une métrique basse (100) et faire passer **tout** le trafic du RPi par la 4G, même sur secteur — ce qui consomme inutilement votre forfait data. Avec la métrique 700, la 4G reste un secours derrière l'Ethernet (`eth0`) et le Wi-Fi (`wlan0`), et n'est utilisée que si les deux tombent. Le réglage est appliqué à chaud *et* persisté pour les prochains branchements.

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

## 🔀 Failover réseau — flux complet

Lorsqu'une coupure de courant est détectée et que le routeur tombe, voici la séquence complète :

```
Coupure détectée (relais GPIO, instantané)
    │
    ▼  attente 30s (configurable, pour laisser le routeur sur onduleur)
    │
    ├── Étape 1 : ping des contrôleurs ReefBeat via Ethernet (eth0)
    │       │
    │       ├── OK → Ethernet fonctionne encore (tous les switchs ont survécu)
    │       │        → réduction intensité pompes via eth0, terminé
    │       │
    │       └── ÉCHEC → Ethernet coupé (un switch entre le RPi et le routeur a lâché)
    │
    ├── Étape 2 : scan Wi-Fi du SSID maison
    │       │
    │       ├── TROUVÉ → le routeur est vivant (sur onduleur) mais un switch est mort
    │       │             → le RPi se connecte au Wi-Fi maison (wlan0)
    │       │             → contrôle des ReefBeat via Wi-Fi
    │       │             → surveillance : si le Wi-Fi tombe plus tard → Étape 3
    │       │
    │       └── ABSENT → le routeur est déjà mort
    │                     → passage à l'Étape 3
    │
    └── Étape 3 : création du hotspot miroir (même SSID + mot de passe sur wlan0)
            │
            ├── Liste blanche MAC : seules les pompes pilotées (MAC relevées
            │    en configuration via /wifi) peuvent s'associer. Sans ça, tous
            │    les appareils Wi-Fi de la maison se rabattent sur le hotspot,
            │    saturent la puce du Pi et empêchent les pompes d'obtenir une IP.
            │
            ├── Les ReefBeat se reconnectent auto au hotspot du RPi
            │    (ils connaissent déjà le SSID/mot de passe)
            │
            ├── Adressage : sur le hotspot, les pompes sont sur 192.168.4.0/24
            │    (≠ du LAN 192.168.0.0/24). Chaque pompe garde son dernier octet
            │    (192.168.0.83 → 192.168.4.83) via une réservation DHCP par MAC.
            │    Le code remappe les IP automatiquement (par MAC, repli sur
            │    substitution d'octet) — pings et commandes suivent les pompes.
            │
            ├── Le RPi pilote les pompes en local via API HTTP
            │
            ├── Attente des pompes : certaines (RSRUN) ne rejoignent qu'après
            │    leur watchdog Wi-Fi (~15 min). Le failover patiente jusqu'à
            │    900s et sort dès que TOUTES sont là, en loggant la progression
            │    (n/total). Le palier d'éco est appliqué immédiatement aux
            │    pompes déjà joignables, sans attendre les retardataires.
            │
            ├── Si modem 4G (E3372h ou tethering USB) disponible :
            │       │
            │       ├── NAT activé : hotspot (wlan0) → 4G (eth1/usb0)
            │       │
            │       ├── ReefBeat → cloud Red Sea → app mobile ✅
            │       │
            │       └── Notifications ntfy.sh via 4G → votre téléphone ✅
            │
            └── Si pas de 4G :
                    └── Contrôle local uniquement (pompes gérées, pas d'internet)


    ⏳ Pendant la coupure, le système surveille en continu :
    │
    ├── SoC batterie → ajuste l'intensité des pompes (eco → survival → critical)
    ├── Disponibilité Wi-Fi → si le Wi-Fi maison réapparaît, rebascule depuis le hotspot
    ├── Connectivité 4G → route les notifications et le trafic ReefBeat
    └── Santé des pompes (health-check) → en mode hotspot, vérifie toutes les
         60s qui répond, re-remappe les IP, et ré-applique le palier d'éco aux
         pompes qui viennent de rejoindre (rattrapage des retardataires)


Retour du courant (relais GPIO, instantané)
    │
    ├── Hotspot désactivé (si actif), règles NAT nettoyées
    │
    ├── IP des pompes restaurées à leurs adresses LAN d'origine (192.168.0.x)
    │
    ├── Le RPi repasse sur Ethernet (eth0) quand les switchs reviennent
    │    (automatique — Linux priorise eth0 sur wlan0)
    │
    ├── Les ReefBeat se reconnectent au Wi-Fi du routeur
    │
    ├── Intensité des pompes restaurée à leur config d'origine (depuis le
    │    snapshot disque). Au retour du courant les pompes peuvent mettre du
    │    temps à rejoindre le Wi-Fi : la restauration retente en arrière-plan
    │    jusqu'à ce que toutes soient restaurées (et peut être relancée à la
    │    main via `restore_pumps.py`).
    │
    ├── Buffer MQTT rejoué → HA reçoit la courbe de décharge complète
    │
    └── Notification ntfy : "Courant rétabli après Xh, SoC Y%"
```

---

## 🛡️ Fiabilité & récupération

Plusieurs mécanismes garantissent que le système reste cohérent même en cas d'imprévu (reboot du Pi en pleine coupure, pompe injoignable au mauvais moment, device lent à rejoindre).

### Snapshots de configuration des pompes

Avant de réduire une pompe, sa configuration d'origine (planning RSRUN, programme de vagues RSWAVE) est sauvegardée sur disque dans `/var/lib/reefbeat-energy-backup/snapshots/`. Au retour du courant, cette config est ré-appliquée exactement, puis le snapshot est supprimé. Un snapshot qui subsiste signifie « restauration encore en attente » — il survit donc à un reboot du Pi.

En complément, un **snapshot de référence** est capturé périodiquement (par défaut toutes les heures) en fonctionnement nominal, dans `/var/lib/reefbeat-energy-backup/reference/`. Ces références ne sont jamais supprimées et servent de filet : si la capture au début d'une coupure échoue (pompe déjà injoignable), le système retombe sur la dernière référence connue.

### Restauration robuste avec retry

Au retour du courant, les pompes Red Sea peuvent mettre du temps à rejoindre le Wi-Fi. La restauration fait une première passe immédiate, puis **retente en arrière-plan** (par défaut toutes les 30s, jusqu'à 40 tentatives) tant que des snapshots subsistent.

Si la restauration automatique échoue (pompes hors ligne trop longtemps), elle peut être relancée à la main :

```bash
python3 restore_pumps.py            # relance la restauration depuis les snapshots
python3 restore_pumps.py --list     # liste les pompes en attente, sans agir
python3 restore_pumps.py --retries 20 --interval 20
```

### Health-check périodique

Le système sonde régulièrement chaque pompe et trace une ligne de synthèse dans les logs, indiquant qui répond, dans quel mode (`auto`/`manual`/`off`) et **depuis quel mode réseau** (client / rejoin / hotspot) :

```
[HEALTH] ✅ 4/4 devices reachable | net=hotspot | battery | RSWAVE45-...=auto, ...
```

La cadence s'adapte : 5 min sur secteur, 15 min sur batterie (économie d'énergie), **60s en mode hotspot** (pour rattraper rapidement une pompe lente à rejoindre). En mode batterie, une pompe qui passe de injoignable à joignable se voit ré-appliquer automatiquement le palier d'éco courant.

### Calcul du SoC robuste

Le coulomb counting borne son pas d'intégration : un cycle de boucle anormalement long (lecture BLE qui timeoute, charge système) ne peut plus provoquer un saut artificiel du SoC. Sur batterie, les lectures BLE bloquantes vers le chargeur (injoignable de toute façon) sont sautées, et la télémétrie chargeur figée est purgée.

> ⚠️ **Conso du Pi non mesurée** : le Pi étant alimenté en aval du shunt INA226 (port 5V de la batterie), sa consommation n'est pas comptée dans le coulomb counting. L'autonomie réelle est donc légèrement inférieure à l'estimation. Voir la note dans le schéma niveau 2.

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
| `sensor.reef_battery_energie_dechargee` | Énergie cumulée sortie de batterie (kWh) — *Energy dashboard* |
| `sensor.reef_battery_energie_chargee` | Énergie cumulée entrée en batterie (kWh) — *Energy dashboard* |
| `sensor.reef_battery_energie_consommee` | Conso totale système depuis l'allumage (kWh) — *Energy dashboard* |

**Si Victron BLE est configuré** (niveau 3) :

| Capteur | Description |
|---|---|
| `sensor.reef_battery_charger_voltage` | Tension de sortie chargeur (V) |
| `sensor.reef_battery_charger_current` | Courant de sortie chargeur (A) |
| `sensor.reef_battery_charger_state` | bulk / absorption / float / storage |
| `sensor.reef_battery_charger_error` | no_error / … |

### Entités de contrôle (test)

Ces entités servent à tester le système sans attendre une vraie coupure :

| Entité | Description |
|---|---|
| `switch.reef_battery_test_plan` | Applique le palier de test aux pompes (vitesse réduite + extinction d'une pompe via `per_device`), sans coupure. OFF = restauration. |
| `button.reef_battery_test_pumps` | Lance un test des commandes pompes à la demande : applique le palier de test, le maintient quelques secondes, puis restaure automatiquement. |
| `number.reef_battery_wifi_cut_min` | Coupe le Wi-Fi du Pi pendant N minutes (0 = aucune) pour observer le failover réseau. Le lien est toujours rétabli automatiquement. |

Ces entités nécessitent `test_level` (et `test_hold_seconds`) configurés dans `config.json`.

### Tableau de bord Énergie & Power Flow Card

Les trois compteurs `energie_dechargee`, `energie_chargee` et `energie_consommee` ont été déclarés avec `device_class: energy` et `state_class: total_increasing` : ils sont **directement éligibles** dans le tableau de bord Énergie de Home Assistant (*Paramètres → Tableaux de bord → Énergie*) :

- **Stockage batterie** → ajoutez la paire `energie_chargee` (entrée) / `energie_dechargee` (sortie).
- **Consommation individuelle** → ajoutez `energie_consommee` pour suivre la conso quotidienne / hebdo / mensuelle du système.

Les compteurs sont persistés sur disque toutes les 60 s, donc un redémarrage ne remet pas les totaux à zéro.

#### Power Flow Card Plus

Pour un visuel temps réel du flux d'énergie (secteur ↔ batterie ↔ aquarium), la carte communautaire [`power-flow-card-plus`](https://github.com/flixlix/power-flow-card-plus) (installable via HACS) fait très bien le travail :

![Power Flow Card](images/power-flow-card.png)

La carte attend une puissance instantanée pour chaque nœud, mais on n'expose qu'une *puissance batterie* signée et de la télémétrie chargeur. Deux template sensors permettent de combler les nœuds « secteur » et « charge » :

```yaml
# configuration.yaml — adapter le préfixe à votre mqtt.device_name
template:
  - sensor:
      # Puissance secteur : sur secteur = sortie chargeur (V × A), sinon 0
      - name: "Reef Backup Puissance Secteur"
        unique_id: reef_backup_grid_power
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
        state: >
          {% if states('sensor.reef_battery_etat_secteur') == 'on_mains' %}
            {% set cv = states('sensor.reef_battery_tension_chargeur') | float(0) %}
            {% set cc = states('sensor.reef_battery_courant_chargeur') | float(0) %}
            {{ (cv * cc) | round(1) }}
          {% else %}
            0
          {% endif %}

      # Puissance consommée (charge réelle de l'aquarium) :
      #   - sur batterie : puissance batterie (positive = décharge)
      #   - sur secteur  : sortie chargeur + puissance batterie (signée :
      #                    négative pendant la charge, donc soustraite)
      - name: "Reef Backup Puissance Charge"
        unique_id: reef_backup_load_power
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
        state: >
          {% set bp = states('sensor.reef_battery_puissance') | float(0) %}
          {% if states('sensor.reef_battery_etat_secteur') == 'on_battery' %}
            {{ [bp, 0] | max | round(1) }}
          {% else %}
            {% set cv = states('sensor.reef_battery_tension_chargeur') | float(0) %}
            {% set cc = states('sensor.reef_battery_courant_chargeur') | float(0) %}
            {{ [(cv * cc) + bp, 0] | max | round(1) }}
          {% endif %}
```

Configuration de la carte :

```yaml
type: custom:power-flow-card-plus
title: Reef Battery Backup
entities:
  battery:
    entity: sensor.reef_battery_puissance
    state_of_charge: sensor.reef_battery_soc_batterie
    name: Batterie LiFePO4
    icon: mdi:battery
    # Convention interne : power > 0 = décharge. La carte attend la
    # convention inverse (power > 0 = charge), d'où invert_state.
    invert_state: true
    display_state: two_way
    show_state_of_charge: true
  home:
    entity: sensor.reef_backup_puissance_charge
    name: Aquarium
    icon: mdi:fishbowl-outline
  grid:
    entity: sensor.reef_backup_puissance_secteur
    name: Secteur
    icon: mdi:transmission-tower
  individual:
    - entity: sensor.reef_battery_intensite_pompes
      name: Pompes
      icon: mdi:pump
      color: "#03a9f4"
      unit_of_measurement: "%"
clickable_entities: true
use_new_flow_rate_model: true
w_decimals: 0
kw_decimals: 2
watt_threshold: 1000
```

> **Note** — adapter le préfixe `reef_battery_*` à votre `mqtt.device_name` (ex. `reef_battery_backup_*`). Si Victron BLE n'est pas installé, le nœud `grid` reste vide en l'absence de mesure secteur — la carte fonctionne tout de même avec uniquement `battery` + `home`.

### Buffer MQTT

Pendant une coupure, HA et le broker MQTT sont presque toujours indisponibles (ils sont sur la même infra que le secteur). Le service écrit toutes les mesures dans `/var/lib/reefbeat-energy-backup/mqtt/messages.jsonl` et les rejoue automatiquement dès que le broker remonte → vous obtenez la courbe complète a posteriori, sans trou.

Configuration optionnelle dans `config.json` :

```json
"mqtt": {
  "buffer_dir": "/var/lib/reefbeat-energy-backup/mqtt",
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
              (Option) Test des commandes pompes : applique brièvement le
              palier de test (vitesse réduite + on/off), vérifie que les
              pompes réagissent, puis restaure — sans coupure
                      │
                      ▼
              Disjoncteur OFF
              SoC / tension / puissance initiaux sauvegardés
              Calcul du forecast (puissance × durée / capacité)
                      │
                      ▼
              (Option) Coupure Wi-Fi pendant N min pour exercer le failover
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

1. Dans Home Assistant, aller dans **Paramètres → Automatisations et Scènes → Blueprints**
2. Cliquer sur **Importer un Blueprint** (en bas à droite)
3. Coller cette URL :
   ```
   https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/refs/heads/main/blueprints/reef_battery_test.yaml
   ```
4. Cliquer **Aperçu** puis **Importer**
5. Aller dans **Automatisations → + Créer une automatisation → Utiliser un Blueprint**
6. Sélectionner **reefbeat⚡Backup — Test Batterie**
7. Renseigner :
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
restore_pumps.py                    Restauration manuelle des pompes (CLI)
mqtt_buffer.py                      Buffer MQTT avec replay
power_estimation.py                 Tables de conso + builder de scénario
ble_scan.py                         Scanner BLE Victron (utilisé par le wizard)
setup.py                            Installeur de dépendances
reefbeat-energy-backup.service   Unité systemd (généré par install.sh)
docs/
  images/                           Images des composants pour la doc
blueprints/
  reef_battery_test.yaml            Blueprint HA de test de batterie
/var/lib/reefbeat-energy-backup/
  snapshots/                        Config pompes pré-coupure (restauration)
  reference/                        Snapshots de référence horaires (filet)
  mqtt/                             Buffer MQTT (replay)
```

---

## ⏰ Redémarrage programmé

Le wizard peut configurer un redémarrage automatique du RPi via cron pour prévenir les problèmes de stabilité à long terme (fuites mémoire, processus zombies). Le redémarrage est **automatiquement annulé si le système est sur batterie** — le script vérifie le GPIO du relais avant de rebooter.

Configuration exemple (via le wizard) :
- Intervalle : tous les jours (1 à 30 jours configurable)
- Heure : 01:00 (n'importe quelle heure au format HH:MM)
- Cron : `/etc/cron.d/reefbeat-reboot`
- Script de vérification : `/usr/local/bin/reefbeat-reboot-check.sh`

Pour désactiver manuellement :

```bash
sudo rm /etc/cron.d/reefbeat-reboot
```

---

## ⚠️ Important : ReefWave et synchronisation cloud

> **Les ReefWave sont « esclaves du cloud »** — ce sont les seuls équipements ReefBeat contrôlés par le cloud Red Sea plutôt qu'en local.

Quand reefbeat⚡Backup modifie le programme de vagues d'une ReefWave pendant une coupure (réduction d'intensité, passage en flux uniforme), il utilise l'**API HTTP locale** qui fonctionne parfaitement — l'appareil change immédiatement de comportement.

Cependant, le **cloud Red Sea et l'app mobile ne sont pas informés** de ce changement. Le cloud croit toujours que la ReefWave exécute son programme d'origine. Concrètement :

**Pendant la coupure :**
- ✅ La ReefWave tourne physiquement à l'intensité réduite (l'API locale fonctionne)
- ✅ Home Assistant voit l'état correct (lecture directe depuis l'appareil)
- ⚠️ L'app mobile ReefBeat affiche l'ancien programme (lecture depuis le cloud)

**Au retour du courant :**
- ✅ reefbeat⚡Backup restaure le programme de vagues original depuis son snapshot
- ✅ L'appareil, Home Assistant et l'app mobile sont de nouveau synchronisés
- ✅ Aucune intervention manuelle nécessaire

**En pratique**, ce n'est pas un problème : pendant une coupure, vous ne gérez pas les programmes de vagues depuis l'app. L'essentiel est que les pompes tournent physiquement à la bonne intensité, et que tout soit restauré correctement au retour du courant.

> 💡 Cette limitation ne concerne que les ReefWave. Les ReefRun (pompes de remontée, skimmers) sont contrôlés localement et restent synchronisés avec l'app en permanence.

### Compatibilité firmware ReefWave (ESP8266 vs ESP32)

Il existe deux générations de ReefWave, et reefbeat⚡Backup gère les deux :

- **ESP32** (firmware récent, ex. `0.10.0`) — beaucoup de mémoire, accepte un programme complet en une seule requête.
- **ESP8266** (firmware plus ancien, ex. `3.0.0`) — mémoire très limitée. Son buffer de parsing JSON est trop petit pour avaler un `POST /auto` contenant 3 intervals ou plus d'un coup : il répond alors `HTTP 400 "could not parse the received JSON"`, **alors même qu'il stocke et exécute parfaitement 5 intervals ou plus** (l'app Red Sea les pousse de façon incrémentale).

Pour être compatible avec les deux, la restauration du programme de vagues **envoie les intervals un par un** à l'intérieur d'un seul cycle d'édition :

```
POST /auto/init       {"uid": op_uid}
POST /auto            {"intervals": [interval_0]}
POST /auto            {"intervals": [interval_1]}
…                     (un POST par interval — l'appareil les accumule)
POST /auto/complete   {"uid": op_uid}
POST /auto/apply      {"uid": op_uid}
```

Chaque requête reste minuscule, donc le buffer de l'ESP8266 suffit, et le firmware empile les intervals. Cette méthode fonctionne aussi sur l'ESP32 : un seul chemin de code pour les deux générations.

> 💡 La même précaution s'applique à l'intégration Home Assistant `ha-reefbeat` (édition d'une vague via l'API locale) : elle pousse également les intervals un par un.

---

## 🐛 Dépannage

Voir [TROUBLESHOOTING.md](TROUBLESHOOTING.md) pour les problèmes courants :

- `Failed to add edge detection` → installer `python3-rpi-lgpio`
- INA226 lit `0.000A` → vérifier le câblage en série du shunt
- Victron `'Scanner' has no attribute 'scan'` → version `victron-ble` incompatible
- MQTT discovery sensors absents → vérifier les credentials et le `base_topic`

**Spécifique au hotspot de secours :**

- Une pompe reste `DOWN` sur le hotspot → vérifier que sa MAC est bien dans `controller_mac_ips` (sinon elle est rejetée par la liste blanche). Relancer `configure.py` pour la (re)collecter via `/wifi`.
- Une pompe met longtemps à rejoindre → certains modèles (RSRUN) ne rebasculent qu'après leur watchdog Wi-Fi (~15 min). Le failover patiente jusqu'à 900s ; laissez le test tourner assez longtemps. Le watchdog est réglable côté Red Sea.
- Les réservations DHCP ne sont pas honorées → la plage DHCP du hotspot doit couvrir les derniers octets des pompes. La migration auto élargit la plage à `.250` au démarrage ; vérifiez `hotspot.dhcp_end` dans `config.json`.
- Logs `[DHCP]` montrant des appareils inconnus → ce sont d'anciens baux ; ils sont désormais purgés à l'activation et seules les pompes whitelistées sont affichées.
- Restauration ReefWave en `HTTP 400 "could not parse the received JSON"` → firmware ESP8266 ancien dont le buffer JSON est trop petit pour un programme multi-intervals envoyé en bloc. La restauration envoie désormais les intervals un par un (voir « Compatibilité firmware ReefWave »), ce qui règle le problème. Mettre à jour le firmware de la pompe via l'app Red Sea est recommandé mais non requis.

> 💡 Au démarrage, le service affiche `[CONFIG] Auto-migrated settings` s'il a complété/corrigé des réglages d'une `config.json` ancienne (plage DHCP, timeout de reconnexion, cadence health-check). Éditez `config.json` pour rendre ces valeurs permanentes.

---

## 📜 Licence

MIT

## 🔗 Projets liés

- [ha-reefbeat-component](https://github.com/Elwinmage/ha-reefbeat-component) — Intégration Home Assistant pour les équipements Red Sea ReefBeat
- [ha-reef-card](https://github.com/Elwinmage/ha-reef-card) — Carte Lovelace HA pour la gestion d'aquarium
