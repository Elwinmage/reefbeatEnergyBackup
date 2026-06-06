# reefbeat⚡Backup

[🇫🇷 Français](README.fr.md) · **🇬🇧 English**

---

Autonomous backup battery monitoring and management system for Red Sea reef aquariums (ReefWave, ReefRun, DC Skimmer, DC Pump).

## ⚡ Features

- **Battery monitoring** via INA226 (I2C, primary) + Victron BLE (optional auxiliary for charger state)
- **Instant outage detection** via 230V relay on GPIO
- **Progressive pump degradation** — SoC-based levels auto-computed from a target autonomy
- **Per-device control** — each ReefWave / ReefRun / Skimmer gets its own intensity per level
- **3-level network failover** — normal Wi-Fi → rejoin → autonomous hotspot
- **Push notifications** — via [ntfy.sh](https://ntfy.sh) (free, no account required) + 4G LTE failover
- **4G internet gateway** — when hotspot is active, routes ReefBeat traffic through 4G so the Red Sea mobile app keeps working
- **Home Assistant integration** — MQTT auto-discovery (10 sensors + charger if Victron)
- **MQTT buffer with replay** — data during HA outage is never lost
- **Auto-detection** — scans your network for ReefBeat devices during setup
- **Self-update** — checks GitHub for new versions, HA update entity with "Install" button
- **Scheduled reboot** — automatic RPi reboot via cron, skipped if on battery
- **Bilingual** — FR/EN interface based on system locale

## 📋 Table of contents

- [Quick install](#-quick-install)
- [Hardware mounting levels](#-hardware-mounting-levels)
  - [Level 1 — Basic setup](#level-1--basic-setup)
  - [Level 2 — Normal setup (recommended)](#level-2--normal-setup-recommended)
  - [Level 3 — Advanced setup](#level-3--advanced-setup)
  - [Increasing autonomy](#increasing-autonomy)
- [Configuration](#-configuration)
- [Home Assistant](#-home-assistant)
- [Battery test blueprint](#-automatic-battery-test-blueprint)
- [Push notifications](#-push-notifications-ntfysh)
- [Project structure](#-project-structure)
- [Troubleshooting](#-troubleshooting)

---

## 🚀 Quick install

```bash
curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | sudo bash
```

The installer:

1. Downloads the latest release
2. Enables I2C on the Pi if needed (`raspi-config nonint do_i2c 0`)
3. Installs `python3-rpi-lgpio` (Pi 5 / kernel 6.6+ compatible) and Python dependencies
4. Launches the interactive wizard which:
   - Scans the network for ReefBeat devices
   - Retrieves Wi-Fi SSID and MAC addresses from your devices
   - Auto-detects your Raspberry Pi model
   - Computes SoC levels from a **target autonomy** (12h, 24h…)
   - Configures battery, INA226 monitoring + optional Victron, MQTT
5. Installs and enables the systemd service
6. Optionally starts the service immediately

## 🔄 Service management

The installer creates a systemd service called `reefbeat-energy-backup`:

```bash
# Status
sudo systemctl status reefbeat-energy-backup

# Start / stop / restart
sudo systemctl start reefbeat-energy-backup
sudo systemctl stop reefbeat-energy-backup
sudo systemctl restart reefbeat-energy-backup

# Live logs
sudo journalctl -u reefbeat-energy-backup -f

# Disable auto-start on boot
sudo systemctl disable reefbeat-energy-backup
```

To reconfigure at any time:

```bash
python3 ~/scripts/reefbeatEnergyBackup/configure.py
sudo systemctl restart reefbeat-energy-backup
```

---

## 🔧 Hardware mounting levels

The system is built in three levels, each adding functionality. You can start at level 1 and upgrade progressively.

### Level 1 — Basic setup

<p align="center">
  <img src="docs/images/level1.png" alt="level1" width="400">
</p>


> **Goal**: provide battery backup for pumps during power outages, without monitoring or automation.

#### 📦 Hardware

| Component | Suggested model | Approx. price |
|---|---|---|
| ![Battery](docs/images/batterie.png) **LiFePO₄ battery 24V 60Ah** *(24V/5A charger included)* | [Kepworth 24V 60Ah](https://www.amazon.fr/dp/B0F3X3LB9K) | ~260 € |
| ![Jack connector](docs/images/jack.png) **Jack adapter for ReefWave** | 5.5 × 2.1 mm barrel jack to bare wires | ~5 € |
| ![RSRun connector](docs/images/rsrun.png) **IP68 4-pin connector for ReefRun/Skimmer** | [IP68 4-pole connector](https://fr.aliexpress.com/item/1005009386771716.html) | ~5 € |
| Wiring (2.5 mm² red/black wire, crimps, heatshrink, 15A fuse) | — | ~20 € |

**Level 1 budget: ~290 €**

> 🔊 **Noise note**: the charger included with the Kepworth battery has an active cooling fan that is relatively noisy. If you plan to install it near a living area, place it further away (utility room, garage) or go directly to [level 3](#level-3--advanced-setup) with the Victron Blue Smart charger, which is much quieter (passive cooling at low charge).

#### 🔌 Wiring diagram

```
                 230 V
                   │
                   ▼
            ┌─────────────┐
            │   Charger   │
            │ 24V 5A inc. │ ← included with battery
            └──────┬──────┘
                   │  24V DC
                   ▼
            ┌─────────────┐
            │   Battery   │
            │  LiFePO₄    │  ← stores energy
            │  24V 60Ah   │
            └──────┬──────┘
                   │  24V DC (with 15A fuse)
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
    ┌───────┐ ┌────────┐ ┌─────────┐
    │ReefRun│ │ReefWave│ │DC Skim. │
    │+pumps │ │  jack  │ │connect. │
    └───────┘ └────────┘ └─────────┘
```

#### 📝 How it works

The principle: **the battery sits in parallel between the charger and the loads**. It is constantly maintained at full charge by the charger (included with the Kepworth battery) in float mode, and automatically supplies power when mains drops — no switch, no electronics in between.

- **ReefWave**: uses the **5.5 × 2.1 mm barrel jack** connector (positive center)
- **ReefRun and DC Skimmer**: use the **IP68 4-pin waterproof connector** proprietary to Red Sea (the pump includes its own regulator, raw 24V is fine)
- The included charger stays plugged in permanently: it automatically switches to float mode once full charge is reached

#### 🔌 Cable fabrication guide

##### ReefWave — 5.5 × 2.1 mm barrel jack

<p align="center">
  <img src="docs/images/jack-polarity.png" alt="Jack polarity +24V center" width="200">
</p>

| Pin | Connection |
|-----|------------|
| **Center pin** (inside) | **+24V** |
| **Outer sleeve** | **GND (−)** |

Standard positive-center polarity. Solder or crimp a 2.5 mm² red wire to the center pin and a black wire to the sleeve.

##### ReefRun / DC Skimmer — IP68 4-pin connector

<p align="center">
  <img src="docs/images/rsrun-pinout.png" alt="IP68 4-pin connector pinout" width="300">
</p>

| Pin | Color | Connection |
|-----|-------|------------|
| **1** (red) | Red | **+24V** |
| **2** (red) | Red | **+24V** |
| **3** (white) | Black | **GND (−)** |
| **4** (white) | Black | **GND (−)** |

> ⚠️ **Important distinction:**
>
> - **DC Skimmer** (single motor): you only need to wire **pins 1 and 3** (+24V and GND). Pins 2 and 4 can be left unconnected.
> - **Return pump (ReefRun)**: you **must wire all 4 pins** — pins 1+2 for +24V, pins 3+4 for GND. The return pump draws more current and uses both pin pairs to distribute the load. Wiring only 2 pins risks overheating the connector.

> 🔴 **CRITICAL — verify with a multimeter before connecting to the battery!**
>
> 1. Set your multimeter to **DC voltage** mode
> 2. Touch the probes to pins 1 (+) and 3 (−) on your assembled cable
> 3. Connect momentarily to the battery
> 4. Verify you read **+24V to +28V** (not negative!)
> 5. A reversed polarity **will destroy** the ReefBeat controller instantly
>
> **Double-check every cable before first use. There is no second chance.**

> ⚠️ **Safety**: a **15A fuse** on the battery + pole, right after the battery, is mandatory. This rating matches the 2.5 mm² cable capacity (~16A max) and provides comfortable margin against typical peak consumption of ~9A (2× ReefWave 45 + ReefRun 12000 + Skimmer + Pi). In case of a short circuit on the load side, this is what saves the battery (and your house).

> 🔧 **DC Skimmer cup full sensor issue**: when powered by the LiFePO₄ battery (26-27V instead of the original 24V), the skimmer's **cup full sensor becomes unreliable** — it triggers false "cup full" alarms even after recalibration. This is a hardware limitation of the probe at higher voltage.
>
> **Recommended fix**: add a [LM2596 DC-DC buck converter](https://www.amazon.fr/dp/B0FLYNNNW) (~2€) between the battery bus and the skimmer only, adjusted to 24.0V output.
>
> <p align="center">
>   <img src="docs/images/lm2596.png" alt="LM2596 DC-DC buck converter" width="200">
> </p>
>
> Wire it to the skimmer's IP68 connector (pins 1+3 only). Then recalibrate the cup sensor via the ReefBeat app. The ReefWave and ReefRun stay connected directly to the battery — they work fine at 26-27V.

#### ✅ What you get

- Power continuity during outages (autonomy ~6-12h depending on your pumps)
- No intervention needed when power cuts
- No monitoring, no degradation: pumps run at 100% until the battery is empty

#### ❌ Limitations

- No visibility on battery state
- No degradation management: battery drains fast, everything shuts off at once
- Risk of repeated deep discharge → accelerated aging

---

### Level 2 — Normal setup *(recommended)*


<p align="center">
  <img src="docs/images/level2.png" alt="level2" width="400">
</p>

> **Goal**: add real-time battery monitoring, automatic outage detection, and progressive pump degradation based on SoC. This is the **recommended** level for a permanent installation.

#### 📦 Additional hardware (on top of level 1)

| Component | Suggested model | Approx. price |
|---|---|---|
| ![INA226](docs/images/ina226.png) **INA226 module 0-36V/20A** (2 mΩ onboard shunt) | [Fasizi INA226 20A](https://www.amazon.fr/dp/B0B7MYYT2V) | ~14 € |
| ![Pi](docs/images/rpi.png) **Raspberry Pi 3 B+** (or newer) | [Pi 3 B+ 1GB at Kubii](https://www.kubii.com/fr/cartes-nano-ordinateurs/2119-raspberry-pi-3-modele-b-1-gb-kubii-5056561800318.html) | ~40 € |
| 16 GB class 10 microSD + Pi USB power supply | — | ~15 € |
| DC-DC converter 24V → 5V 3A for the Pi | Step-down buck regulator | ~8 € |
| ![Finder](docs/images/finder.png) **Finder 40.61.8.230.4000 relay** (230V coil, 1 NO/NC) | [Finder 40.61](https://www.amazon.fr/dp/B003A611AE) | ~12 € |
| ![Support](docs/images/support.png) **Finder 95.95.3 DIN socket** | [Finder 95.95.3](https://www.amazon.fr/dp/B0018L99AC) | ~8 € |
| 35 mm DIN rail (10 cm) + small electrical enclosure | — | ~15 € |

**Additional budget: ~112 €** — **Cumulative level 2 budget: ~402 €**

#### 🔌 Wiring diagram

```
                 230 V ─────┬───────────────┐
                            │               │
                            ▼               ▼
                     ┌─────────────┐   ┌──────────┐
                     │   Charger   │   │  Relay   │
                     │ Victron 24V │   │  Finder  │
                     └──────┬──────┘   │   40.61  │
                            │ 24V      │  coil    │
                            ▼          │   230V   │
                     ┌─────────────┐   └────┬─────┘
                     │   Battery   │        │ NO/NC
              ┌──────┤  LiFePO₄    │        │ contact
              │      └──────┬──────┘        │
              │             │ 24V           │
              │      [Shunt INA226]         │
              │             │               │
              │             ▼               │
              │    ┌────────────────┐       │
              │    │ DC-DC 24V→5V  │       │
              │    └────────┬───────┘       │
              │             │ 5V            │
              │             ▼               │
              │    ┌────────────────┐       │
              ├────│  Raspberry Pi  │◄──────┘
              │I2C │   GPIO 26     │ GPIO state
              │SDA │   GPIO 2 SDA  │
              │SCL │   GPIO 3 SCL  │
              │    └────────────────┘
              │
              ▼
       ReefRun / ReefWave / DC Skimmer
```

#### 📝 Wiring details

**INA226 shunt wiring** (most important):

The INA226 module must be **in series on the battery + pole**, between the battery and all loads. This is what allows it to measure net current in/out.

```
Battery (+) ──► [IN+ shunt INA226 IN−] ──► Bus + 24V ─┬─► Charger (output)
                                                        ├─► DC-DC to Pi
                                                        ├─► ReefRun
                                                        ├─► ReefWave
                                                        └─► DC Skimmer

Battery (−) ──────────────────────────► Bus − (common)
```

The shunt sees:
- **positive current** = battery is discharging (supplying loads)
- **negative current** = battery is charging (from charger)

**Outage detection relay wiring**:

The Finder 40.61.8.230 is a **mains voltage absence detector**: its coil is powered by 230V, its NO/NC contacts switch when mains drops.

| Socket 95.95.3 terminal | Connection |
|---|---|
| A1 | 230V Phase |
| A2 | 230V Neutral |
| 11 (common) | Pi GND |
| 12 (NC) | Pi GPIO 26 (with internal pull-up) |

Mains OK → coil energized → NC contact open → GPIO reads 1 (pulled to 3.3V).
Outage → coil drops → NC contact closes → GPIO pulled to GND, reads 0.

**Pi → INA226 connections** (4 wires):

| Pi GPIO | INA226 |
|---|---|
| Pin 1 (3.3V) | VCC |
| Pin 6 (GND) | GND |
| Pin 3 (GPIO 2 SDA) | SDA |
| Pin 5 (GPIO 3 SCL) | SCL |

#### ✅ What you get

- **Real-time monitoring**: battery voltage, current, power, SoC via coulomb counting
- **Outage detection in < 1 second** via the relay
- **Automatic degradation**: ReefWaves drop to 70%, then 50%, then 10% as SoC decreases; skimmer turns off in survival mode; etc.
- **Configuration snapshots**: at outage, original pump config is saved to disk; on power restore, it's restored identically
- **MQTT buffer**: while HA is down (which almost always happens during a real outage), measurements are stored locally and replayed when the broker comes back → you get the **complete discharge curve** in HA
- **Network failover**: if the Wi-Fi router also goes down, the Pi switches to hotspot mode to stay reachable

---

### Level 3 — Advanced setup


<p align="center">
  <img src="docs/images/level3.png" alt="level3" width="400">
</p>

> **Goal**: add remote charger monitoring, a connected circuit breaker for **scheduled discharge tests**, and 4G LTE backup for notifications when all networks are down.
>
> The three additions of this level are **independent** — you can install any combination of them:

| Addition | Purpose | Can be installed alone? |
|---|---|---|
| 🔌 **Victron BLE charger** | Silent charger + charger state in HA | ✅ Yes |
| ⚡ **Connected circuit breaker** | Automated discharge tests from HA | ✅ Yes |
| 📶 **4G LTE module** | Notifications + ReefBeat cloud access when Wi-Fi is down | ✅ Yes |

#### 📦 Additional hardware (on top of level 2)

| Component | Suggested model | Approx. price |
|---|---|---|
| ![Charger BLE](docs/images/chargeur.png) **Victron Blue Smart IP22 24/12** *(replaces Kepworth charger — silent + BLE)* | [Victron Blue Smart IP22 24/12](https://www.amazon.fr/dp/B08P4Z8NL6) | ~155 € |
| ![Circuit breaker](docs/images/disjoncteur.png) **Connected Wi-Fi circuit breaker 16A with meter** | [Tongou TO-Q-SY1-JWT](https://www.amazon.fr/dp/B08ND2RGX8) | ~30 € |
| ![SIM7600G-H](docs/images/sim7600g-h.png) **SIM7600G-H 4G HAT** *(recommended — integrated on the Pi, RNDIS)* | [Kubii SIM7600G-H HAT](https://www.kubii.com/fr/hat-cartes-d-extensions/3296-module-hat-lte-cat-4-4g-3g-2g-pour-raspberry-pi-3272496306189.html) | ~75 € |
| ![DC-DC 5V](docs/images/dcdc-5v.png) **DC-DC 24V→5V 5A module** *(required if using SIM7600 HAT — the RPi USB alone can't power both)* | [DC-DC Buck 9-36V to 5.2V 5A](https://www.amazon.fr/dp/B0F9FLF6QB) | ~2.50 € |

*Or alternatively to the SIM7600:*

| Component | Suggested model | Approx. price |
|---|---|---|
| ![Huawei E3372h](docs/images/huawei-e3372h-320.png) **Huawei E3372h-320 4G USB dongle** *(plug-and-play, no extra power needed)* | [Huawei E3372h-320](https://www.amazon.fr/HUAWEI-51071SMK-Huawei-E3372h-320-LTE-Stick/dp/B085RDTZMP) | ~40 € |
| **USB tethering from a smartphone** *(no extra hardware, phone powered via RPi USB)* | — | 0 € |

**Maximum additional budget: ~262 €** (all three with SIM7600) — **Cumulative level 3 budget: ~664 €**

#### 🔌 Wiring diagram

```
        230 V ──► [Tongou Wi-Fi breaker] ──┬──────────────┐
                                            │              │
                                            ▼              ▼
                                     ┌─────────────┐   ┌──────────┐
                                     │  Charger    │   │  Relay   │
                                     │Victron BLE  │   │  Finder  │
                                     │24/12 Smart  │   │detection │
                                     └──────┬──────┘   └────┬─────┘
                                            │ 24V           │
                                            ▼               │
                                     ┌─────────────┐        │
                                     │   Battery   │◄───[shunt INA226]
                                     └──────┬──────┘        │
                                            │ 24V           │
                                            ▼               │
                                      (loads)               │
                                                            │
                                      ┌──── Wi-Fi ───┐      │
                                      │              │      │
                                      ▼              ▼      │
                                Home Assistant   Raspberry Pi
                                (Tongou            GPIO 26 ◄┘
                                 integration)       │
                                      │           USB │
                                      │ BLE          ▼
                                      ▼        ┌───────────┐
                                Victron charger │  Huawei   │
                                (real-time)     │ E3372h    │
                                                │  4G LTE   │
                                                └───────────┘
```

#### 📝 Details

**Tongou TO-Q-SY1-JWT circuit breaker**:

A DIN-rail modular breaker controlled via Wi-Fi (Tuya protocol, integrable with HA via [Local Tuya](https://github.com/rospogrigio/localtuya) or the official Tuya Cloud integration). It also provides real-time kWh / V / A measurement — useful to verify the charger switches to battery when simulating an outage.

**Wiring**: the breaker is installed **just before** the Victron charger and the Finder relay. When you switch it off from HA, it's exactly like a real power outage:

- The charger stops providing power
- The Finder relay sees the voltage absence → contact switches
- The Pi sees the outage via GPIO and immediately triggers degradation

**Victron Blue Smart IP22 24/12 charger (with BLE)**:

Replaces the Kepworth charger included with the battery. Besides adding Bluetooth Low Energy, **it is significantly quieter**: passive cooling at low charge, the fan only kicks in at full charge above 8A. Ideal if the system is installed in a living area.

Publishes to HA:
- Charger state (`storage` / `bulk` / `absorption` / `float`)
- Real-time output voltage and current
- Error codes (overheat, battery voltage out of range…)

Configuration: retrieve the **encryption key** from the VictronConnect app (Settings → Product Info → Instant Readout → "Show"), enter it in the configuration wizard.

**SIM7600G-H 4G HAT** *(recommended)*:

<p align="center">
  <img src="docs/images/sim7600g-h.png" alt="SIM7600G-H 4G HAT" width="300">
</p>

The [SIM7600G-H HAT](https://www.kubii.com/fr/hat-cartes-d-extensions/3296-module-hat-lte-cat-4-4g-3g-2g-pour-raspberry-pi-3272496306189.html) (~75€) plugs directly onto the Raspberry Pi's GPIO header. LTE Cat4 150 Mbps, global bands, with GNSS positioning.

The wizard automatically configures it in **RNDIS mode** — the module appears as a USB network interface (`usb0`) with DHCP. No PPP, QMI, or AT commands needed for data after initial setup.

**First-time setup** (handled by the wizard):
1. Configure APN: `AT+CGDCONT=1,"IP","your_apn"`
2. Switch to RNDIS: `AT+CUSBPIDSWITCH=9011,1,1`
3. Module reboots → `usb0` appears with IP via DHCP

After initial setup, the module boots automatically in RNDIS mode at every RPi startup — fully autonomous.

> ⚡ **Power note**: the RPi's built-in USB port can power the Pi alone (~2.1A), but **not both the Pi and the SIM7600 HAT**. When using the SIM7600, power the RPi through a [DC-DC 24V→5V 5A buck module](https://www.amazon.fr/dp/B0F9FLF6QB) (~2.50€) connected to the battery bus, feeding the RPi's GPIO pins 2 (5V) and 6 (GND) — not the USB-C port.

**Testing:** `python3 test_sim7600.py` runs a complete diagnostic (serial, SIM, signal, network, connectivity).

**LTE monitoring**: every 10 minutes (configurable), the system queries the modem via AT commands and publishes to HA: signal strength (dBm), signal quality, operator, network type (4G/3G/2G), SIM status, modem model, firmware, IMEI, IP, and connectivity status.

##### Huawei E3372h-320 alternative

<p align="center">
  <img src="docs/images/huawei-e3372h-320.png" alt="Huawei E3372h-320" width="300">
</p>

The [E3372h-320](https://www.amazon.fr/HUAWEI-51071SMK-Huawei-E3372h-320-LTE-Stick/dp/B085RDTZMP) (~40€) is a simpler plug-and-play option. Just plug it into the Pi's USB port with an active SIM card — it creates a virtual Ethernet interface (`eth1`), no configuration needed. HiLink web interface at `http://192.168.8.1`.

> 🔑 **SIM PIN**: during setup, the wizard detects if the SIM requires a PIN, enters it, and proposes to **disable it permanently**. Strongly recommended — without it, the modem cannot reconnect after a power cycle.

##### USB tethering alternative

If you don't want to buy a modem, you can use a **smartphone connected via USB** as a 4G/5G modem. Enable USB tethering on the phone (Settings → Network → Hotspot → USB tethering), plug it into the RPi, and the wizard will detect it. The phone is powered via USB from the RPi (which is on battery), so it stays charged during the outage.

**4G internet gateway for ReefBeat devices** *(all three LTE options)*: when the RPi hotspot is active, the RPi acts as a NAT router — it forwards internet traffic from the ReefBeat devices (connected to the hotspot) through 4G. The **Red Sea mobile app keeps working** during a power outage.

#### ✅ What you get

- **Remote mains control** to the battery from HA
- **Scheduled discharge tests**: see the [blueprint section](#-automatic-battery-test-blueprint)
- **Full charger visibility** (mode, current, errors)
- **Total consumption measurement** in kWh via the Tongou breaker
- **Notifications even when everything is down** via 4G LTE
- **Red Sea mobile app keeps working** during outages (4G gateway)
- **LTE telemetry in HA** — signal, operator, network type, SIM status, IMEI

---

### Increasing autonomy

> **Goal**: double (or more) battery capacity for longer outages.

The simplest and safest method is adding one or more **identical batteries in parallel**. LiFePO₄ batteries with internal BMS (like the Kepworth 24V 60Ah) natively support parallel operation.

#### 📦 Hardware per additional battery

| Component | Approx. price |
|---|---|
| 1× identical LiFePO₄ 24V 60Ah battery | ~260 € |
| 2× interconnect cables 2.5 mm² (50 cm red + 50 cm black, crimped) | ~10 € |
| 1× inline **15A fuse** (one per additional battery) | ~3 € |

**Budget per +60 Ah: ~273 €**

#### 🔌 Parallel wiring diagram

```
                Bus + (to charger and loads)
                      ▲
                      │
        ┌─────────────┼─────────────┐
        │             │             │
   [fuse]        [fuse]        [fuse]
   15 A          15 A          15 A
        │             │             │
   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐
   │ Bat #1  │   │ Bat #2  │   │ Bat #3  │
   │24V 60Ah │   │24V 60Ah │   │24V 60Ah │
   └────┬────┘   └────┬────┘   └────┬────┘
        │             │             │
        └─────────────┼─────────────┘
                      │
                      ▼
                Bus − (common)
```

#### 📝 Important rules

1. **Identical batteries only**: same brand, same model, ideally same age. Mixing batteries of different capacities or ages overworks the weakest one → accelerated aging.
2. **Initial balancing**: before connecting in parallel, charge each battery individually to 100% and verify they are at the same voltage (±0.1V). Otherwise, equalization will occur through high current between batteries → risk of melting crimps.
3. **Equal cross-section cables**: if one battery has a longer or thinner cable, it will discharge less → permanent imbalance.
4. **One fuse per battery**, not a single common fuse: if one battery fails, only that one is isolated.
5. **No change to the INA226 shunt**: it stays on the common bus and sees the **total** combined current from both batteries — exactly what we want for SoC.

#### 📊 Cumulative capacities and estimated autonomies

For a typical setup (2× ReefWave 45 + 1× ReefRun 12000 + DC Skimmer + Pi):

| Configuration | Usable capacity | 24h target autonomy |
|---|---|---|
| 1× 60 Ah | 1228 Wh | achievable (estimated 32h) |
| 2× 60 Ah | 2457 Wh | comfortable (estimated 60h+) |
| 3× 60 Ah | 3686 Wh | luxurious (90h+) |

> ⚠️ Re-run the `configure.py` wizard after adding a battery to update the total capacity in `config.json`. The scenario calculation will account for it automatically.

---

## ⚙️ Configuration

The `configure.py` wizard is interactive and bilingual (FR/EN based on locale). It guides through several steps:

1. **Network** — Wi-Fi SSID confirmation (read from NetworkManager)
2. **ReefBeat device detection** — automatic subnet scan, select devices to back up
3. **Outage detection** — relay GPIO (recommended) or current monitoring
4. **Battery** — pack capacity (Ah)
5. **Monitoring** — INA226 (mandatory, auto-detected on I2C) + Victron BLE (optional)
6. **Backup mode** — choose between:
   - **Auto** (recommended): set a target autonomy, the wizard detects the Pi, asks about auxiliary loads, and computes optimal SoC levels + intensities
   - **Simple**: a single backup speed for everything
7. **MQTT** — Home Assistant connection settings
8. **Push notifications** — ntfy.sh topic + 4G LTE failover (E3372h or tethering) + NAT gateway
9. **Scheduled reboot** — automatic RPi reboot via cron (skipped if on battery)
10. **Polling interval**

The result is saved in `config.json` and can be edited manually if needed.

---

## 🏠 Home Assistant

### Auto-published sensors

All sensors appear automatically in HA after MQTT discovery configs are published.

| Sensor | Description |
|---|---|
| `sensor.reef_battery_voltage` | Battery voltage (V) |
| `sensor.reef_battery_current` | Current (A, + = discharging) |
| `sensor.reef_battery_power` | Power (W) |
| `sensor.reef_battery_soc` | State of Charge (%) |
| `sensor.reef_battery_power_state` | mains / battery |
| `sensor.reef_battery_pump_intensity` | Average pump intensity (%) |
| `sensor.reef_battery_runtime` | Estimated runtime (h) — always shows "if power cuts now" |
| `sensor.reef_battery_outage_duration` | Current outage duration (min) |
| `sensor.reef_battery_network_mode` | client / rejoin / hotspot |
| `sensor.reef_battery_monitor_source` | ina226 |

**If Victron BLE is configured** (level 3):

| Sensor | Description |
|---|---|
| `sensor.reef_battery_charger_voltage` | Charger output voltage (V) |
| `sensor.reef_battery_charger_current` | Charger output current (A) |
| `sensor.reef_battery_charger_state` | bulk / absorption / float / storage |
| `sensor.reef_battery_charger_error` | no_error / … |

**If 4G LTE is configured** (level 3):

| Sensor | Description |
|---|---|
| `sensor.reef_battery_lte_signal` | Signal strength (dBm) |
| `sensor.reef_battery_lte_signal_quality` | Signal quality (0-31 RSSI scale) |
| `sensor.reef_battery_lte_operator` | Operator name (e.g. Orange F) |
| `sensor.reef_battery_lte_network_type` | Network type (LTE/HSPA+/EDGE/GSM) |
| `sensor.reef_battery_lte_sim_status` | SIM status (ready/pin_required/absent) |
| `sensor.reef_battery_lte_connected` | Internet reachable via 4G (ON/OFF) |
| `sensor.reef_battery_lte_ip` | LTE interface IP address |
| `sensor.reef_battery_lte_model` | Modem model |
| `sensor.reef_battery_lte_manufacturer` | Modem manufacturer |
| `sensor.reef_battery_lte_firmware` | Modem firmware version |
| `sensor.reef_battery_lte_imei` | Modem IMEI |

LTE sensors are updated every 10 minutes (configurable via `lte_monitor.check_interval_min` in `config.json`).

### MQTT buffer

During an outage, HA and the MQTT broker are almost always unavailable (they're on the same infrastructure as the mains). The service writes all measurements to `/var/lib/reefbeat-energy-backup/mqtt/messages.jsonl` and replays them automatically when the broker comes back → you get the complete curve after the fact, with no gaps.

Optional configuration in `config.json`:

```json
"mqtt": {
  "buffer_dir": "/var/lib/reefbeat-energy-backup/mqtt",
  "buffer_retention_days": 7
}
```

---

## 🔀 Network failover — complete flow

When a power outage is detected and the home router goes down, here is the complete sequence:

```
Power outage detected (relay GPIO, instant)
    │
    ▼  wait 30s (configurable, for router UPS to stabilize)
    │
    ├── Step 1: ping ReefBeat controllers via Ethernet (eth0)
    │       │
    │       ├── OK → Ethernet still works (all switches survived)
    │       │        → reduce pump intensity via eth0, done
    │       │
    │       └── FAIL → Ethernet down (a switch between RPi and router lost power)
    │
    ├── Step 2: scan Wi-Fi for home SSID
    │       │
    │       ├── FOUND → router is alive (on UPS) but a switch died
    │       │           → RPi connects to home Wi-Fi (wlan0)
    │       │           → controls ReefBeat via Wi-Fi
    │       │           → monitors: if Wi-Fi drops later → go to Step 3
    │       │
    │       └── NOT FOUND → router is already dead
    │                        → go to Step 3
    │
    └── Step 3: create mirror hotspot (same SSID + password on wlan0)
            │
            ├── ReefBeat devices auto-reconnect to RPi hotspot
            │    (they already know the SSID/password)
            │
            ├── RPi controls pumps locally via HTTP API
            │
            ├── If 4G modem (E3372h or USB tethering) is available:
            │       │
            │       ├── NAT enabled: hotspot (wlan0) → 4G (eth1/usb0)
            │       │
            │       ├── ReefBeat → Red Sea cloud → mobile app ✅
            │       │
            │       └── ntfy.sh notifications via 4G → your phone ✅
            │
            └── If no 4G:
                    └── Local control only (pumps managed, no internet)


    ⏳ During the outage, the system continuously monitors:
    │
    ├── Battery SoC → adjusts pump intensity (eco → survival → critical)
    ├── Wi-Fi availability → if home Wi-Fi reappears, switch back from hotspot
    └── 4G connectivity → route notifications and ReefBeat traffic


Power restored (relay GPIO, instant)
    │
    ├── Hotspot deactivated (if active), NAT rules cleaned
    │
    ├── RPi reconnects to Ethernet (eth0) when switches come back
    │    (automatic — Linux prioritizes eth0 over wlan0)
    │
    ├── ReefBeat devices reconnect to home router Wi-Fi
    │
    ├── Pump intensity restored to 100%
    │
    ├── MQTT buffer replayed → HA gets the complete discharge curve
    │
    └── ntfy notification: "Power restored after Xh, SoC Y%"
```

---

## 📱 Push notifications (ntfy.sh)

Receive alerts directly on your phone without Home Assistant, using the free [ntfy.sh](https://ntfy.sh) service.

**Setup:**
1. Install the ntfy app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347))
2. Subscribe to your topic (configured during wizard setup)
3. That's it — notifications are sent automatically on power outage

**Notification events** (only triggered during outages):
- ⚡ Power outage detected (with SoC and estimated runtime)
- ✅ Power restored (with outage duration)
- 🟡🟠🔴 Pump level changes (eco → survival → critical)
- 🚨 Battery critically low (repeated alerts every 60s)
- 📡 Network failover status

**Priorities:** outage = `high` (sound), critical = `urgent` (persistent alarm), info = `default` (silent)

**Test from the command line:**

```bash
python3 test_notif.py                     # Simple test
python3 test_notif.py --type outage       # Simulate power outage
python3 test_notif.py --type critical     # Simulate critical battery (alarm)
python3 test_notif.py --type restored     # Simulate power restored
python3 test_notif.py --lte              # Force send via 4G modem
python3 test_notif.py --message "Hello"   # Custom message
```

---

## 🤖 Automatic battery test blueprint

> **Available only with level 3** (Tongou circuit breaker required).

This Home Assistant blueprint periodically triggers a **real discharge test**: it switches off the mains breaker for 40 minutes, observes the discharge curve, and compares it to the forecast computed by the scenario.

### How it works

```
Scheduled date (e.g.: last Sunday of the month, every 3 months)
      │
      ▼
Is "user" detected at home?
      │
      ├─── No ──► Test silently cancelled
      │
      └─── Yes
              │
              ▼
        Actionable HA notification on phone
        "Run battery test for 40 min?"
        (no timeout: waits for explicit response)
              │
              ├─── Decline ──────────────► Cancelled
              │
              └─── Accept
                      │
                      ▼
              Breaker OFF
              Initial SoC / voltage / power saved
              Forecast computed (power × duration / capacity)
                      │
                      ▼
              Wait 40 min, OR immediate abort if voltage < threshold
              (service switches to battery mode,
               MQTT buffer records everything)
                      │
                      ▼
              Breaker ON
                      │
                      ▼
              3-axis analysis:
                📊 Forecast: actual SoC consumed vs prediction
                🔋 Voltage profile: final voltage in LFP plateau?
                ⏱  Extrapolated autonomy to 20% SoC
                      │
                      ▼
              Summary notification to phone + HA log
```

### Blueprint installation

1. In Home Assistant, go to **Settings → Automations & Scenes → Blueprints**
2. Click **Import Blueprint** (bottom right)
3. Paste this URL:
   ```
   https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/refs/heads/main/blueprints/reef_battery_test.yaml
   ```
4. Click **Preview** then **Import**
5. Go to **Automations → + Create Automation → Use a Blueprint**
6. Select **reefbeat⚡Backup — Battery Test**
7. Fill in: schedule, person, notification service, breaker switch, SoC/voltage/power sensors, battery capacity, test duration, tolerance, emergency voltage threshold

### Important precautions

⚠️ **Never run a test with nobody home**: if the battery is in poor condition or the scenario is miscalibrated, the test could cause total pump shutdown after the 40 minutes. A human must be able to intervene manually.

⚠️ **First use**: run a **manual** test first (flip the breaker by hand for 5-10 min) to verify the entire system reacts correctly before scheduling automated 40-min tests.

⚠️ **Timing**: avoid feeding hours for fish/corals. Choose a quiet time slot.

---

## 📁 Project structure

```
install.sh                          Installer (curl | bash)
configure.py                        Interactive wizard
config.example.json                 Default template
config.json                         Your configuration (generated by wizard)
main.py                             Main service loop
monitor.py                          INA226 backend + Victron BLE auxiliary
outage.py                           Outage detection (relay GPIO)
hotspot.py                          3-level network failover
controller.py                       Pump control + outage orchestration
notifier.py                         Push notifications (ntfy.sh + 4G LTE)
lte_monitor.py                      LTE modem telemetry (MQTT sensors)
test_notif.py                       CLI notification tester
test_reefbeat.py                    CLI ReefBeat equipment tester
test_sim7600.py                     CLI SIM7600G-H 4G module tester
updater.py                          Self-update module (GitHub + HA update entity)
update.py                           CLI update tool
VERSION                             Current version number
mqtt_buffer.py                      MQTT buffer with replay
power_estimation.py                 Power tables + scenario builder
ble_scan.py                         Victron BLE scanner (used by wizard)
setup.py                            Dependency installer
docs/
  images/                           Component images for documentation
blueprints/
  reef_battery_test.yaml            HA battery test blueprint
```

---

## 🔧 CLI tools

| Command | Description |
|---------|-------------|
| `python3 configure.py` | Reconfigure (re-run the wizard) |
| `python3 test_reefbeat.py` | Test ReefBeat device communication |
| `python3 test_reefbeat.py --read` | Read current state of all devices |
| `python3 test_reefbeat.py --test-all` | Full test cycle (read → change → verify → restore) |
| `python3 test_sim7600.py` | Test SIM7600G-H 4G module (serial, SIM, signal, connectivity) |
| `python3 test_notif.py` | Test push notifications |
| `python3 update.py` | Check for updates |
| `python3 update.py --install` | Install available update |
| `python3 ble_scan.py` | Scan for Victron BLE devices |
| `python3 setup.py --check` | Verify dependencies and hardware |

---

## ⏰ Scheduled reboot

The wizard can set up an automatic RPi reboot via cron to prevent long-term stability issues (memory leaks, zombie processes). The reboot is **skipped automatically if the system is running on battery** — it checks the relay GPIO before rebooting.

Configuration example (via wizard):
- Interval: every day (1-30 days configurable)
- Time: 01:00 (any HH:MM)
- Cron job: `/etc/cron.d/reefbeat-reboot`
- Check script: `/usr/local/bin/reefbeat-reboot-check.sh`

To disable manually:

```bash
sudo rm /etc/cron.d/reefbeat-reboot
```

---

## 🔄 Updates

### From Home Assistant

An `update` entity appears automatically in HA (`update.reef_battery_update`). It shows the current and latest version, with an **Install** button when an update is available — just like any HA add-on.

The service checks GitHub every 6 hours (configurable). After clicking "Install", the update is downloaded, config.json is backed up, and the service restarts automatically.

### From the command line

```bash
cd ~/scripts/reefbeatEnergyBackup

# Check for updates
python3 update.py

# Install update
python3 update.py --install

# Force reinstall
python3 update.py --force

# Show current version
python3 update.py --version
```

---

## ⚠️ Important: ReefWave and cloud synchronization

> **ReefWave devices are "cloud-slave"** — they are the only ReefBeat devices controlled by the Red Sea cloud rather than locally.

When reefbeat⚡Backup changes a ReefWave's wave program during an outage (reducing intensity, switching to uniform flow), it uses the **local HTTP API** which works perfectly — the device changes its behavior immediately.

However, the **Red Sea cloud and mobile app are unaware of this change**. The cloud still believes the ReefWave is running its original schedule. This means:

**During an outage:**
- ✅ The ReefWave physically runs at the reduced intensity (local API works)
- ✅ Home Assistant sees the correct state (reads from the device directly)
- ⚠️ The ReefBeat mobile app shows the old schedule (reads from the cloud)

**When power is restored:**
- ✅ reefbeat⚡Backup restores the original wave program from its snapshot
- ✅ The device, Home Assistant, and the mobile app are all back in sync
- ✅ No manual intervention needed

**In practice**, this is not a problem: during an outage, you're not managing wave programs from the app anyway. The important thing is that the pumps physically run at the right intensity, and that everything is restored correctly when power returns.

> 💡 This limitation only affects ReefWave. ReefRun (return pumps, skimmers) are controlled locally and stay in sync with the app at all times.

---

## 🐛 Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues:

- `Failed to add edge detection` → install `python3-rpi-lgpio`
- INA226 reads `0.000A` → verify shunt is wired in series
- Victron `'Scanner' has no attribute 'scan'` → incompatible `victron-ble` version
- MQTT discovery sensors missing → check credentials and `base_topic`
- `runtime_h` shows `-1.0` → update to latest version (fixed)

---

## 📜 License

MIT

## 🔗 Related projects

- [ha-reefbeat-component](https://github.com/Elwinmage/ha-reefbeat-component) — Home Assistant integration for Red Sea ReefBeat devices
- [ha-reef-card](https://github.com/Elwinmage/ha-reef-card) — Lovelace card for reef tank management
