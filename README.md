# reefbeat⚡Backup

**🇬🇧 English** · [🇫🇷 Français](README.fr.md)

---

Standalone backup-battery monitoring and management system for Red Sea reef aquariums (ReefWave, ReefRun, DC Skimmer, DC Pump).

## ⚡ Features

- **Battery monitoring** via INA226 (I2C, primary) + Victron BLE (optional auxiliary for charger state)
- **Sub-second outage detection** via 230 V relay on GPIO
- **Progressive pump dimming** — SoC levels computed automatically from a target autonomy
- **Per-device control** — every ReefWave / ReefRun / Skimmer gets its own intensity per level
- **3-tier network failover** — normal Wi-Fi → reconnect → standalone hotspot
- **Home Assistant integration** — MQTT auto-discovery (10 sensors + charger if Victron)
- **MQTT buffer with replay** — measurements taken during an HA outage are never lost
- **Auto-detection** — scans the network for ReefBeat devices during setup
- **Bilingual wizard** — FR/EN based on the system locale

## 📋 Table of contents

- [Quick install](#-quick-install)
- [Hardware build levels](#-hardware-build-levels)
  - [Level 1 — Bare-bones build](#level-1--bare-bones-build)
  - [Level 2 — Standard build (recommended)](#level-2--standard-build-recommended)
  - [Level 3 — Advanced build](#level-3--advanced-build)
  - [Increasing autonomy](#increasing-autonomy)
- [Configuration](#-configuration)
- [Home Assistant](#-home-assistant)
- [Battery test blueprint](#-automatic-battery-test-blueprint)
- [Project structure](#-project-structure)
- [Troubleshooting](#-troubleshooting)

---

## 🚀 Quick install

```bash
curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | sudo bash
```

The installer will:

1. Download the latest version
2. Enable I2C on the Pi if needed (`raspi-config nonint do_i2c 0`)
3. Install `python3-rpi-lgpio` (for Pi 5 / kernel 6.6+ compatibility) and Python dependencies
4. Run an interactive wizard that:
   - Scans the network for ReefBeat devices
   - Reads Wi-Fi SSID and MAC addresses from your devices
   - Auto-detects your Raspberry Pi model
   - Builds SoC levels from a **target autonomy** (12 h, 24 h…)
   - Configures battery, INA226 monitoring + optional Victron, MQTT

---

## 🔧 Hardware build levels

The system is built up in three levels, each adding capabilities. Start at level 1 and grow into level 3 over time if you like.

### Level 1 — Bare-bones build

> **Goal**: keep the pumps powered from the battery during a mains outage. No monitoring, no automation.

#### 📦 Bill of materials

| Component | Suggested model | Indicative price |
|---|---|---|
| ![Battery](docs/images/batterie.png) **24 V 60 Ah LiFePO₄ battery** *(comes with a 24V/5A charger)* | [Kepworth 24V 60Ah](https://www.amazon.fr/dp/B0F3X3LB9K) | ~260 € |
| ![Jack connector](docs/images/jack.png) **ReefWave jack adaptor cable** | 5.5 × 2.1 mm jack to bare wires | ~5 € |
| ![RSRun connector](docs/images/rsrun.png) **IP68 4-pin connector for ReefRun/Skimmer** | [IP68 4-pole connector](https://fr.aliexpress.com/item/1005009386771716.html) | ~5 € |
| Wiring (2.5 mm² red/black, lugs, heat-shrink, 15 A fuse) | — | ~20 € |

**Level 1 budget: ~290 €**

> 🔊 **Noise warning**: the charger bundled with the Kepworth battery uses an active cooling fan that's quite loud. If you plan to install it in a cabinet near a living area, prefer a remote location (utility room, basement, garage) — or jump straight to [level 3](#level-3--advanced-build) with the Victron Blue Smart charger which is much quieter (passive cooling under light load).

#### 🔌 Wiring diagram

```
                 230 V
                   │
                   ▼
            ┌─────────────┐
            │   Charger   │
            │ 24V 5A inc. │ ← bundled with the battery
            └──────┬──────┘
                   │  24 V DC
                   ▼
            ┌─────────────┐
            │   Battery   │
            │  LiFePO₄    │  ← stores energy
            │  24V 60Ah   │
            └──────┬──────┘
                   │  24 V DC (with 15 A fuse)
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
    ┌───────┐ ┌────────┐ ┌─────────┐
    │ReefRun│ │ReefWave│ │DC Skim. │
    │+pumps │ │  jack  │ │connect. │
    └───────┘ └────────┘ └─────────┘
```

#### 📝 How it works

The principle: **the battery sits in parallel between the charger and the loads**. It's permanently kept charged by the bundled Kepworth charger in float mode, and starts feeding the loads automatically when mains drops — no switch, no electronics in the middle.

- **ReefWave** uses a **5.5 × 2.1 mm jack** (centre positive)
- **ReefRun and DC Skimmer** use Red Sea's proprietary **IP68 4-pin connector** (the pump has its own regulator, so raw 24 V is fine)
- The bundled charger stays on the battery permanently: it switches to float mode automatically once the battery is full

> ⚠️ **Safety**: a **15 A fuse** on the battery's + pole, right at the battery output, is mandatory. This rating is matched to the 2.5 mm² cable (~16 A max) and gives a comfortable margin over the typical ~9 A peak draw (2× ReefWave 45 + ReefRun 12000 + Skimmer + Pi). In case of a short on the load side, this is what saves the battery (and your house).

#### ✅ What you get

- Power continuity during outages (autonomy ~6-12 h depending on your pumps)
- Zero intervention required when the outage hits
- No monitoring, no graceful degradation: the pumps run at 100% until the battery is empty

#### ❌ Limitations

- No visibility on the battery state
- No graceful degradation: the battery drains fast, then everything goes dark at once
- Risk of repeated deep discharges → accelerated ageing

---

### Level 2 — Standard build *(recommended)*

> **Goal**: add real-time battery monitoring, automatic outage detection, and progressive pump dimming based on SoC. This is the **recommended** level for a long-term install.

#### 📦 Additional bill of materials (on top of level 1)

| Component | Suggested model | Indicative price |
|---|---|---|
| ![INA226](docs/images/ina226.png) **INA226 0-36V/20A module** (2 mΩ shunt onboard) | [Fasizi INA226 20A](https://www.amazon.fr/dp/B0B7MYYT2V) | ~14 € |
| ![Pi](docs/images/rpi.png) **Raspberry Pi 3 B+** (or newer) | [Pi 3 B+ 1 GB at Kubii](https://www.kubii.com/fr/cartes-nano-ordinateurs/2119-raspberry-pi-3-modele-b-1-gb-kubii-5056561800318.html) | ~40 € |
| 16 GB class 10 microSD card + Pi USB power supply | — | ~15 € |
| 24 V → 5 V 3 A DC-DC step-down converter for the Pi | Buck regulator | ~8 € |
| ![Finder relay](docs/images/finder.png) **Finder 40.61.8.230.4000 relay** (230 V coil, 1 NO/NC) | [Finder 40.61](https://www.amazon.fr/dp/B003A611AE) | ~12 € |
| ![Finder socket](docs/images/support.png) **Finder 95.95.3 DIN socket** | [Finder 95.95.3](https://www.amazon.fr/dp/B0018L99AC) | ~8 € |
| 35 mm DIN rail (10 cm) + small electrical enclosure | — | ~15 € |

**Additional budget: ~112 €** — **Cumulative level 2: ~402 €**

#### 🔌 Wiring diagram

```
                 230 V ─────┬───────────────┐
                            │               │
                            ▼               ▼
                     ┌─────────────┐   ┌──────────┐
                     │   Charger   │   │  Finder  │
                     │ Victron 24V │   │   relay  │
                     └──────┬──────┘   │   40.61  │
                            │ 24V      │   coil   │
                            ▼          │   230V   │
                     ┌─────────────┐   └────┬─────┘
                     │   Battery   │        │ NO/NC
              ┌──────┤  LiFePO₄    │        │ contact
              │      └──────┬──────┘        │
              │             │ 24V           │
              │      [INA226 shunt]         │
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

#### 📝 How it works

**INA226 shunt wiring** (most important):

The INA226 module must be **in series on the battery's + pole**, between the battery and all the loads. That's what lets it measure the net current flowing in or out of the battery.

```
Battery (+) ──► [IN+ INA226 shunt IN−] ──► +24V bus ─┬─► Charger output
                                                      ├─► DC-DC to Pi
                                                      ├─► ReefRun
                                                      ├─► ReefWave
                                                      └─► DC Skimmer

Battery (−) ────────────────────────────► − bus (common)
```

The shunt then sees:
- **positive current** = battery is discharging (powering the loads)
- **negative current** = battery is charging (from the Victron)

**Outage-detection relay wiring**:

The Finder 40.61.8.230 relay is a **mains-presence detector**: its coil runs on 230 V, its NO/NC contacts toggle when mains drops.

| 95.95.3 socket terminal | Connection |
|---|---|
| A1 | 230 V live |
| A2 | 230 V neutral |
| 11 (common) | Pi GND |
| 12 (NC) | Pi GPIO 26 (with internal pull-up) |

When mains is OK, the coil is energised → NC contact is open → GPIO reads 1 (pulled up to 3.3 V).
On outage, the coil drops → NC contact closes → GPIO is pulled to GND, reads 0.

**Pi → INA226 connections** (4 wires):

| Pi GPIO | INA226 |
|---|---|
| Pin 1 (3.3 V) | VCC |
| Pin 6 (GND) | GND |
| Pin 3 (GPIO 2 SDA) | SDA |
| Pin 5 (GPIO 3 SCL) | SCL |

#### ✅ What you get

- **Real-time monitoring**: battery voltage, current, power, SoC computed via coulomb counting
- **Sub-second outage detection** via the relay
- **Automatic dimming**: ReefWave pumps step down to 70%, then 50%, then 10% as SoC drops; the skimmer stops in survival mode; etc.
- **Configuration snapshots**: at outage, every pump's original config is saved on disk; on return, it's restored exactly as it was
- **MQTT buffer**: while HA is down (which almost always happens during a real outage), measurements are stored locally and replayed once the broker comes back → you get the **complete discharge curve** in HA
- **Network failover**: if the Wi-Fi router dies too, the Pi switches to its own hotspot to stay reachable

---

### Level 3 — Advanced build

> **Goal**: add remote charger control and a connected breaker to run **scheduled discharge tests** from Home Assistant.

#### 📦 Additional bill of materials (on top of level 2)

| Component | Suggested model | Indicative price |
|---|---|---|
| ![BLE charger](docs/images/chargeur.png) **Victron Blue Smart IP22 24/12** charger *(replaces the bundled Kepworth charger — silent + BLE)* | [Victron Blue Smart IP22 24/12](https://www.amazon.fr/dp/B08P4Z8NL6) | ~155 € |
| ![Breaker](docs/images/disjoncteur.png) **Wi-Fi 16 A connected breaker with metering** | [Tongou TO-Q-SY1-JWT](https://www.amazon.fr/dp/B08ND2RGX8) | ~30 € |

**Additional budget: ~185 €** (Victron BLE charger replacing the bundled one + connected breaker)

**Cumulative level 3: ~587 €**

#### 🔌 Wiring diagram

```
        230 V ──► [Tongou Wi-Fi breaker] ──┬──────────────┐
                                            │              │
                                            ▼              ▼
                                     ┌─────────────┐   ┌──────────┐
                                     │ Charger     │   │  Finder  │
                                     │Victron BLE  │   │  relay   │
                                     │24/12 Smart  │   │ detector │
                                     └──────┬──────┘   └────┬─────┘
                                            │ 24 V          │
                                            ▼               │
                                     ┌─────────────┐        │
                                     │   Battery   │◄───[INA226 shunt]
                                     └──────┬──────┘        │
                                            │ 24 V          │
                                            ▼               │
                                          (loads)           │
                                                            │
                                          ┌──── Wi-Fi ───┐  │
                                          │              │  │
                                          ▼              ▼  │
                                    Home Assistant   Raspberry Pi
                                    (Tongou             GPIO 26 ◄┘
                                     integration)
                                          │
                                          │ BLE
                                          ▼
                                    Victron charger
                                    (live state)
```

#### 📝 How it works

**Tongou TO-Q-SY1-JWT breaker**:

DIN-modular breaker controlled over Wi-Fi (Tuya protocol, integrates with HA via [Local Tuya](https://github.com/rospogrigio/localtuya) or the official Tuya Cloud integration). It also provides live kWh / V / A metering — handy to verify that the charger really swings to battery feed when you simulate an outage.

**Wiring**: the breaker is installed **right before** the Victron charger and the Finder relay. When you trip it from HA, it's exactly like a real mains outage:

- The charger stops delivering anything
- The Finder relay sees no mains → its contact toggles
- The Pi sees the outage on GPIO and triggers degradation immediately

**Victron Blue Smart IP22 24/12 charger (with BLE)**:

Replaces the Kepworth charger that came bundled with the battery. On top of adding Bluetooth Low Energy, **it's noticeably quieter**: passive cooling under light load, the fan only kicks in during heavy charging above 8 A. Perfect if the system sits in a living area.

Reports to HA:
- Charger state (`storage` / `bulk` / `absorption` / `float`)
- Live output voltage and current
- Any errors (overheat, battery voltage out of range…)

Configuration: grab the **encryption key** from the VictronConnect app (Settings → Product Info → Instant Readout → "Show"), to enter in the configuration wizard.

#### ✅ What you get

- **Remote mains control** to the battery from HA
- **Scheduled discharge tests**: see the [blueprint section](#-automatic-battery-test-blueprint)
- **Full charger visibility** (mode, current, errors)
- **Total consumption metering** in kWh via the Tongou breaker (useful for real autonomy figures)

---

### Increasing autonomy

> **Goal**: double (or more) battery capacity for longer outages.

The simplest and safest way is to add one or more **identical batteries in parallel**. LiFePO₄ batteries with internal BMS (like the Kepworth 24V 60Ah) accept this natively.

#### 📦 Per-additional-battery BoM

| Component | Indicative price |
|---|---|
| 1× identical 24V 60Ah LiFePO₄ battery | ~260 € |
| 2× 2.5 mm² jumper cables (50 cm red + 50 cm black, crimped lugs) | ~10 € |
| 1× **15 A inline fuse** (one per additional battery) | ~3 € |

**Per +60 Ah budget: ~273 €**

#### 🔌 Parallel wiring diagram

```
                + bus (to charger and loads)
                      ▲
                      │
        ┌─────────────┼─────────────┐
        │             │             │
   [fuse 15A]   [fuse 15A]    [fuse 15A]
        │             │             │
   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐
   │ Bat #1  │   │ Bat #2  │   │ Bat #3  │
   │24V 60Ah │   │24V 60Ah │   │24V 60Ah │
   └────┬────┘   └────┬────┘   └────┬────┘
        │             │             │
        └─────────────┼─────────────┘
                      │
                      ▼
                − bus (common)
```

#### 📝 Important rules

1. **Identical batteries only**: same brand, same model, ideally same age. Mixing capacities or ages forces the weaker pack to overwork → accelerated ageing.
2. **Initial balancing**: before paralleling, charge each battery individually to 100% and check they're at the same voltage (±0.1 V). Otherwise, balancing will happen via a strong inrush current at connect time → risk of melting the lugs.
3. **Equal-section jumper cables**: if one battery has a longer or thinner cable, it'll deliver less current → permanent imbalance.
4. **One fuse per battery**, not a single shared fuse: in case of a fault on one battery, only that one is isolated.
5. **No INA226 modification**: it stays on the common bus, so it sees the **total** current of all batteries combined — exactly what we want for SoC.

#### 📊 Combined capacities and estimated autonomy

For a typical setup (2× ReefWave 45 + 1× ReefRun 12000 + DC Skimmer + Pi):

| Configuration | Usable capacity | 24 h target |
|---|---|---|
| 1× 60 Ah | 1228 Wh | reachable (~32 h) |
| 2× 60 Ah | 2457 Wh | comfortable (~60 h+) |
| 3× 60 Ah | 3686 Wh | luxurious (~90 h+) |

> ⚠️ Re-run `configure.py` after adding a battery so the new total capacity is picked up in `config.json`. The scenario builder will use it automatically.

---

## ⚙️ Configuration

The `configure.py` wizard is interactive and bilingual (FR/EN based on locale). It walks through 6 steps:

1. **Network** — confirm the home Wi-Fi SSID (read from NetworkManager)
2. **ReefBeat device discovery** — auto-scan the local subnet, pick the devices to put on battery
3. **Outage detection** — pick between GPIO relay (recommended) and current monitoring
4. **Battery** — pack capacity in Ah
5. **Monitoring** — INA226 (mandatory, auto-detected on I2C) + Victron BLE (optional)
6. **Backup mode** — pick between:
   - **Auto** (recommended): give a target autonomy, the wizard detects the Pi, asks for auxiliary loads, and computes optimal SoC levels and pump intensities
   - **Simple**: a single backup speed across the board

The result is saved to `config.json` and can be edited by hand if needed.

---

## 🏠 Home Assistant

### Auto-published sensors

All sensors appear automatically in HA after MQTT discovery is published.

| Sensor | Description |
|---|---|
| `sensor.reef_battery_voltage` | Battery voltage (V) |
| `sensor.reef_battery_current` | Current (A, + = discharging) |
| `sensor.reef_battery_power` | Power (W) |
| `sensor.reef_battery_soc` | State of Charge (%) |
| `sensor.reef_battery_power_state` | mains / battery |
| `sensor.reef_battery_pump_intensity` | Average pump intensity (%) |
| `sensor.reef_battery_runtime` | Estimated runtime (h) |
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

### MQTT buffer

During an outage, HA and the MQTT broker are almost always unavailable (they sit on the same infrastructure as mains). The service writes every measurement to `/var/lib/reef-battery-monitor/mqtt/messages.jsonl` and replays them automatically once the broker comes back → you get the full curve after the fact, with no gaps.

Optional configuration in `config.json`:

```json
"mqtt": {
  "buffer_dir": "/var/lib/reef-battery-monitor/mqtt",
  "buffer_retention_days": 7
}
```

---

## 🤖 Automatic battery-test blueprint

> **Available only with level 3** (Tongou breaker required).

This Home Assistant blueprint periodically triggers a **real discharge test**: it opens the mains breaker for 40 minutes, watches the discharge, and compares it to the forecast computed by the scenario.

### How it works

```
Scheduled date (e.g. last Sunday of the month, every 3 months)
      │
      ▼
Is "user_y" detected at home?
      │
      ├─── No ──► Test silently cancelled
      │
      └─── Yes
              │
              ▼
        Actionable HA notification on phone
        "Run 40-minute battery test?"
        (no timeout: waits for an explicit answer)
              │
              ├─── Deny ─────────────────► Cancelled
              │
              └─── Accept
                      │
                      ▼
              Breaker OFF
              Initial SoC / voltage / power saved
              Forecast computed (power × duration / capacity)
                      │
                      ▼
              Wait 40 minutes, OR abort immediately
              if voltage drops below safety threshold
              (the service falls back to battery,
               the MQTT buffer captures everything)
                      │
                      ▼
              Breaker ON
                      │
                      ▼
              Three-axis analysis:
                📊 Forecast: actual SoC drop vs prediction
                🔋 Voltage profile: still in the LFP plateau?
                ⏱  Extrapolated runtime down to 20% SoC
                      │
                      ▼
              Phone summary notification + HA log
```

### Installing the blueprint

The blueprint is shipped under [`blueprints/reef_battery_test.yaml`](blueprints/reef_battery_test.yaml).

To install in HA:

1. Copy the file to `<config>/blueprints/automation/reefbeat/reef_battery_test.yaml`
2. Reload blueprints in HA (Settings → Automations → ⋮ → Reload)
3. Create a new automation from this blueprint
4. Fill in:
   - **Time of day** (e.g. 14:00) — avoid feeding hours
   - **Weekday**: Monday through Sunday
   - **Occurrence**: 1st, 2nd, 3rd, 4th, or **last** (recommended for weekends)
   - **Period (months)**: 1, 3 or 6 months between tests
   - **Person whose presence is required**: e.g. `person.elwin`
   - **Notification service**: e.g. `mobile_app_pixel_8` (without the `notify.` prefix)
   - **Connected breaker**: the Tongou switch entity
   - **SoC / voltage / power sensor**: `sensor.reef_battery_*`
   - **Battery capacity (Wh)**: e.g. 1228 for 60Ah × 25.6V × 0.8 DoD
   - **Test duration (min)**: 40 by default
   - **Forecast deviation tolerance (% SoC)**: 3 by default
   - **Emergency voltage floor (V)**: 24.0 by default
   - **LFP plateau lower bound (V)**: 25.6 by default

### Important precautions

⚠️ **Never run a test with nobody at home**: if the battery is in poor health or the scenario is mis-calibrated, the test could lead to all pumps stopping after the 40 minutes. A human must be able to step in.

⚠️ **First use**: do a **manual** test first (open the breaker by hand for 5-10 min) to verify the whole system reacts correctly before unleashing 40-minute automated tests.

⚠️ **Timing**: avoid feeding hours for fish/corals. Pick a quiet slot.

---

## 📁 Project structure

```
install.sh                          One-line installer (curl | bash)
configure.py                        Interactive wizard
config.example.json                 Default template
config.json                         Your configuration (generated by the wizard)
main.py                             Service main loop
monitor.py                          INA226 backend + Victron BLE auxiliary
outage.py                           Outage detection (relay GPIO)
hotspot.py                          3-tier network failover
controller.py                       Pump control + outage orchestration
mqtt_buffer.py                      MQTT buffer with replay
power_estimation.py                 Power tables + scenario builder
ble_scan.py                         Victron BLE scanner (used by the wizard)
setup.py                            Dependency installer
reef-battery-monitor.service        systemd unit
docs/
  images/                           Component images for the docs
blueprints/
  reef_battery_test.yaml            HA battery-test blueprint
```

---

## 🐛 Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common problems:

- `Failed to add edge detection` → install `python3-rpi-lgpio`
- INA226 reads `0.000A` → check the shunt is wired in series
- Victron `'Scanner' has no attribute 'scan'` → incompatible `victron-ble` version
- MQTT discovery sensors missing → check credentials and `base_topic`

---

## 📜 License

MIT

## 🔗 Related projects

- [ha-reefbeat-component](https://github.com/Elwinmage/ha-reefbeat-component) — Home Assistant integration for Red Sea ReefBeat devices
- [ha-reef-card](https://github.com/Elwinmage/ha-reef-card) — Lovelace card for HA aquarium management
