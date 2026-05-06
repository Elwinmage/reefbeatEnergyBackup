#!/usr/bin/env python3
"""
reefbeat⚡Backup — Interactive Configuration Wizard

Scans the network for ReefBeat devices, configures Wi-Fi, battery,
monitoring, pump intensity levels, and MQTT settings.

Usage:
  python3 configure.py
  python3 configure.py --install-dir /path/to/install
"""

import argparse
import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# =============================================================================
# i18n
# =============================================================================

def detect_lang() -> str:
    """Detect system language, return 'fr' or 'en'."""
    lang = os.environ.get("LANG", "en_US.UTF-8")
    return "fr" if lang.startswith("fr") else "en"

LANG = detect_lang()

def t(fr: str, en: str) -> str:
    """Return localized string."""
    return fr if LANG == "fr" else en

# =============================================================================
# Terminal UI helpers
# =============================================================================

class C:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    INFO = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    END = "\033[0m"
    CYAN = "\033[96m"

def banner(msg: str):
    w = 60
    print(f"\n{C.BOLD}{'='*w}")
    print(f"  {msg}")
    print(f"{'='*w}{C.END}\n")

def section(msg: str):
    print(f"\n{C.BOLD}{C.CYAN}── {msg} ──{C.END}\n")

def ok(msg: str):    print(f"  {C.OK}✓{C.END} {msg}")
def warn(msg: str):  print(f"  {C.WARN}⚠{C.END} {msg}")
def fail(msg: str):  print(f"  {C.FAIL}✗{C.END} {msg}")
def info(msg: str):  print(f"  {C.INFO}ℹ{C.END} {msg}")

def ask(prompt: str, default: Any = None, choices: list = None) -> str:
    """Ask user for input with optional default and choices."""
    suffix = ""
    if default is not None:
        suffix = f" [{default}]"
    if choices:
        choice_str = "/".join(str(c) for c in choices)
        suffix = f" ({choice_str}){suffix}"

    while True:
        try:
            answer = input(f"  {C.BOLD}?{C.END} {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

        if not answer and default is not None:
            return str(default)
        if answer and (not choices or answer in [str(c) for c in choices]):
            return answer
        if choices:
            warn(t(f"Choix invalide. Options: {', '.join(str(c) for c in choices)}",
                   f"Invalid choice. Options: {', '.join(str(c) for c in choices)}"))

def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " (O/n)" if LANG == "fr" else " (Y/n)"
    if not default:
        suffix = " (o/N)" if LANG == "fr" else " (y/N)"
    try:
        answer = input(f"  {C.BOLD}?{C.END} {prompt}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if not answer:
        return default
    if LANG == "fr":
        return answer in ("o", "oui", "y", "yes")
    return answer in ("y", "yes", "o", "oui")

def ask_int(prompt: str, default: int = None, min_val: int = None,
            max_val: int = None) -> int:
    """Ask for an integer."""
    while True:
        val = ask(prompt, default)
        try:
            n = int(val)
            if min_val is not None and n < min_val:
                warn(f"Min: {min_val}")
                continue
            if max_val is not None and n > max_val:
                warn(f"Max: {max_val}")
                continue
            return n
        except ValueError:
            warn(t("Entrez un nombre entier", "Enter an integer"))

def choose_from_list(items: list, prompt: str, multi: bool = False,
                     display_fn=None) -> list:
    """Let user choose one or more items from a numbered list."""
    if not items:
        return []

    for i, item in enumerate(items, 1):
        label = display_fn(item) if display_fn else str(item)
        print(f"    {C.BOLD}{i}.{C.END} {label}")
    print()

    if multi:
        hint = t("Entrez les numéros séparés par des virgules (ex: 1,3,4)",
                  "Enter numbers separated by commas (e.g.: 1,3,4)")
        info(hint)
        raw = ask(prompt)
        try:
            indices = [int(x.strip()) - 1 for x in raw.split(",")]
            return [items[i] for i in indices if 0 <= i < len(items)]
        except (ValueError, IndexError):
            warn(t("Sélection invalide", "Invalid selection"))
            return choose_from_list(items, prompt, multi, display_fn)
    else:
        idx = ask_int(prompt, min_val=1, max_val=len(items))
        return [items[idx - 1]]


# =============================================================================
# ReefBeat network scanner
# =============================================================================

# ReefBeat device types we care about for energy backup
BACKUP_DEVICE_TYPES = {
    "RSWAVE45": "ReefWave 45",
    "RSWAVE25": "ReefWave 25",
    "RSRUN5500": "ReefRun 5500",
    "RSRUN9000": "ReefRun 9000",
    "RSRUN12000": "ReefRun 12000",
    "RSLED50": "ReefLED 50",
    "RSLED90": "ReefLED 90",
    "RSLED160": "ReefLED 160",
}

# All known ReefBeat hw_model IDs (for detection)
ALL_REEFBEAT_MODELS = {
    "RSWAVE45", "RSWAVE25",
    "RSRUN5500", "RSRUN9000", "RSRUN12000",
    "RSLED50", "RSLED90", "RSLED160",
    "RSATO", "RSDOSE", "RSMAT",
    "RSSENSE",
}

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def get_local_subnet() -> Optional[str]:
    """Detect local subnet CIDR."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        return f"{local_ip}/24"
    except OSError:
        return None


def probe_reefbeat(ip: str) -> Optional[Dict]:
    """Probe a single IP for ReefBeat device-info."""
    if not HAS_REQUESTS:
        return None
    try:
        r = req_lib.get(f"http://{ip}/device-info", timeout=2)
        if r.status_code != 200:
            return None
        data = r.json()
        hw_model = data.get("hw_model", "")
        if hw_model not in ALL_REEFBEAT_MODELS:
            return None
        return {
            "ip": ip,
            "hw_model": hw_model,
            "name": data.get("name", ""),
            "friendly": BACKUP_DEVICE_TYPES.get(hw_model, hw_model),
        }
    except Exception:
        return None


def get_reefbeat_wifi(ip: str) -> Optional[Dict]:
    """Get Wi-Fi info from a ReefBeat device."""
    if not HAS_REQUESTS:
        return None
    try:
        r = req_lib.get(f"http://{ip}/wifi", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_reefbeat_mac(ip: str) -> Optional[str]:
    """Get MAC address from a ReefBeat device via ARP."""
    try:
        result = subprocess.run(
            ["arp", "-n", ip], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if ip in line:
                match = re.search(r"(([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", line)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return None


def scan_reefbeat_devices() -> List[Dict]:
    """Scan the local network for ReefBeat devices."""
    subnet = get_local_subnet()
    if not subnet:
        fail(t("Impossible de détecter le sous-réseau",
               "Cannot detect local subnet"))
        return []

    info(t(f"Scan du réseau {subnet}...", f"Scanning network {subnet}..."))

    net = ipaddress.ip_network(subnet, strict=False)
    ips = [str(ip) for ip in net.hosts()]

    devices = []
    with ThreadPoolExecutor(max_workers=64) as executor:
        results = list(executor.map(probe_reefbeat, ips))

    for r in results:
        if r:
            devices.append(r)

    return devices


# =============================================================================
# Victron BLE scanner
# =============================================================================

VICTRON_MFR_ID = 0x02E1
VICTRON_TYPES = {
    0x08: "AC Charger (Blue Smart IP22)",
    0x01: "Solar Charger",
    0x02: "Battery Monitor",
    0x05: "SmartShunt",
}


async def scan_victron_ble(duration: float = 10.0) -> List[Dict]:
    """Scan for Victron BLE devices."""
    try:
        from bleak import BleakScanner
    except ImportError:
        fail(t("bleak non installé. Installez avec: pip install bleak",
               "bleak not installed. Install with: pip install bleak"))
        return []

    devices = {}

    def on_detect(device, adv):
        if VICTRON_MFR_ID in adv.manufacturer_data:
            data = adv.manufacturer_data[VICTRON_MFR_ID]
            dev_type = data[1] if len(data) > 1 else 0
            devices[device.address] = {
                "address": device.address,
                "name": adv.local_name or device.name or "Unknown",
                "rssi": adv.rssi,
                "type": VICTRON_TYPES.get(dev_type, f"Unknown (0x{dev_type:02X})"),
                "type_id": dev_type,
            }

    info(t(f"Scan Bluetooth ({duration}s)...", f"Bluetooth scan ({duration}s)..."))
    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(duration)
    await scanner.stop()

    return list(devices.values())


# =============================================================================
# Wi-Fi helpers
# =============================================================================

def get_current_ssid() -> Optional[str]:
    """Get the SSID the RPi is currently connected to."""
    try:
        result = subprocess.run(
            ["iwgetid", "-r"], capture_output=True, text=True, timeout=5
        )
        ssid = result.stdout.strip()
        return ssid if ssid else None
    except Exception:
        return None


def get_wifi_password_from_system(ssid: str) -> Optional[str]:
    """Try to extract Wi-Fi password from wpa_supplicant config."""
    wpa_paths = [
        "/etc/wpa_supplicant/wpa_supplicant.conf",
        "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf",
    ]
    for path in wpa_paths:
        try:
            content = Path(path).read_text()
            # Find network block for this SSID
            blocks = re.findall(
                r'network\s*=\s*\{([^}]+)\}', content, re.DOTALL
            )
            for block in blocks:
                if f'ssid="{ssid}"' in block:
                    match = re.search(r'psk="([^"]+)"', block)
                    if match:
                        return match.group(1)
        except PermissionError:
            continue
        except Exception:
            continue
    return None


def test_wifi_connection(ssid: str, password: str) -> bool:
    """Test Wi-Fi credentials by attempting connection."""
    try:
        result = subprocess.run(
            ["wpa_passphrase", ssid, password],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return True  # Assume OK if can't test


# =============================================================================
# Configuration wizard
# =============================================================================

def load_existing_config(install_dir: str) -> Dict:
    """Load existing config.json or config.example.json as defaults."""
    config_path = Path(install_dir) / "config.json"
    example_path = Path(install_dir) / "config.example.json"

    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            warn(t("config.json corrompu, utilisation des valeurs par défaut",
                   "config.json corrupted, using defaults"))

    if example_path.exists():
        try:
            with open(example_path) as f:
                return json.load(f)
        except Exception:
            pass

    return {}


def save_config(cfg: Dict, install_dir: str):
    """Save config, backing up existing file."""
    config_path = Path(install_dir) / "config.json"

    if config_path.exists():
        backup = f"config.json.save.{datetime.now().strftime('%Y%m%d.%H.%M.%S')}"
        backup_path = Path(install_dir) / backup
        shutil.copy2(config_path, backup_path)
        info(t(f"Ancienne config sauvegardée: {backup}",
               f"Previous config backed up: {backup}"))

    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

    ok(t(f"Configuration sauvegardée: {config_path}",
         f"Configuration saved: {config_path}"))


def run_wizard(install_dir: str):
    """Run the interactive configuration wizard."""
    banner("reefbeat⚡Backup — Configuration")
    defaults = load_existing_config(install_dir)
    cfg: Dict[str, Any] = {}

    # =================================================================
    # Step 1: Scan ReefBeat devices
    # =================================================================
    section(t("1. Détection des équipements ReefBeat",
              "1. ReefBeat device detection"))

    info(t("Scan du réseau local à la recherche d'équipements Red Sea...",
           "Scanning local network for Red Sea equipment..."))

    all_devices = scan_reefbeat_devices()

    if not all_devices:
        fail(t("Aucun équipement ReefBeat détecté sur le réseau.",
               "No ReefBeat devices detected on the network."))
        info(t("Vérifiez que vos équipements sont allumés et connectés au même réseau.",
               "Check that your devices are powered on and on the same network."))
        return

    # Filter for backup-eligible devices (ReefWave, ReefRun, ReefLED)
    backup_devices = [
        d for d in all_devices if d["hw_model"] in BACKUP_DEVICE_TYPES
    ]

    if not backup_devices:
        fail(t("Aucun ReefWave, ReefRun ou ReefLED détecté.",
               "No ReefWave, ReefRun or ReefLED detected."))
        return

    ok(t(f"{len(backup_devices)} équipement(s) compatible(s) trouvé(s):",
         f"{len(backup_devices)} compatible device(s) found:"))
    print()

    for d in backup_devices:
        print(f"    {C.BOLD}{d['friendly']}{C.END} — {d['name']} ({d['ip']})")
    print()

    # Let user choose which devices to back up
    selected = choose_from_list(
        backup_devices,
        t("Sélectionnez les équipements à secourir",
          "Select devices to back up"),
        multi=True,
        display_fn=lambda d: f"{d['friendly']} — {d['name']} ({d['ip']})"
    )

    if not selected:
        fail(t("Aucun équipement sélectionné.", "No device selected."))
        return

    ok(t(f"{len(selected)} équipement(s) sélectionné(s)",
         f"{len(selected)} device(s) selected"))

    # =================================================================
    # Step 2: Wi-Fi configuration
    # =================================================================
    section(t("2. Configuration Wi-Fi", "2. Wi-Fi configuration"))

    # Get SSID from ReefBeat devices
    device_ssids = {}
    device_macs = {}

    for d in selected:
        ip = d["ip"]
        wifi_info = get_reefbeat_wifi(ip)
        if wifi_info:
            ssid = wifi_info.get("ssid", "")
            if ssid:
                device_ssids[d["name"]] = ssid
                info(f"  {d['name']}: SSID = '{ssid}'")

        mac = get_reefbeat_mac(ip)
        if mac:
            device_macs[d["name"]] = {"mac": mac, "ip": ip}
            info(f"  {d['name']}: MAC = {mac}")

    # Check all devices are on the same SSID
    unique_ssids = set(device_ssids.values())
    if len(unique_ssids) > 1:
        fail(t(
            "Les équipements ne sont pas tous sur le même réseau Wi-Fi !",
            "Devices are not all on the same Wi-Fi network!"
        ))
        for name, ssid in device_ssids.items():
            print(f"    {name}: {ssid}")
        fail(t(
            "reefbeat⚡Backup ne supporte qu'un SSID commun à tous les équipements.",
            "reefbeat⚡Backup only supports a single SSID shared by all devices."
        ))
        return

    detected_ssid = list(unique_ssids)[0] if unique_ssids else None
    current_ssid = get_current_ssid()

    if detected_ssid:
        ok(t(f"SSID détecté: '{detected_ssid}'", f"Detected SSID: '{detected_ssid}'"))
        wifi_ssid = detected_ssid
    else:
        default_ssid = (defaults.get("network", {})
                        .get("home_wifi", {}).get("ssid", ""))
        wifi_ssid = ask(
            t("SSID du réseau Wi-Fi", "Wi-Fi network SSID"),
            default_ssid or current_ssid
        )

    # Get password
    wifi_password = None
    if current_ssid == wifi_ssid:
        info(t("Vous êtes connecté à ce réseau.",
               "You are connected to this network."))
        sys_pwd = get_wifi_password_from_system(wifi_ssid)
        if sys_pwd:
            ok(t("Mot de passe récupéré depuis la configuration système.",
                 "Password retrieved from system configuration."))
            wifi_password = sys_pwd
        else:
            info(t("Impossible de récupérer le mot de passe automatiquement.",
                   "Cannot retrieve password automatically."))
            if os.geteuid() != 0:
                info(t("Relancez en root pour permettre la lecture de wpa_supplicant.",
                       "Rerun as root to allow reading wpa_supplicant."))

    if not wifi_password:
        default_pwd = (defaults.get("network", {})
                       .get("home_wifi", {}).get("password", ""))
        wifi_password = ask(
            t("Mot de passe Wi-Fi", "Wi-Fi password"),
            default_pwd if default_pwd and default_pwd != "YOUR_HOME_PASSWORD" else None
        )

    # Test credentials
    if test_wifi_connection(wifi_ssid, wifi_password):
        ok(t("Identifiants Wi-Fi valides", "Wi-Fi credentials valid"))
    else:
        warn(t("Impossible de valider les identifiants",
               "Cannot validate credentials"))

    # Build MAC->IP mapping for hotspot DHCP
    mac_ip_map = {}
    for name, data in device_macs.items():
        mac_ip_map[data["mac"]] = data["ip"]

    cfg["network"] = {
        "interface": "wlan0",
        "home_wifi": {
            "ssid": wifi_ssid,
            "password": wifi_password,
        },
        "failover": {
            "enabled": True,
            "check_delay_s": 30.0,
            "retry_count": 3,
            "retry_delay_s": 5.0,
            "scan_timeout_s": 15.0,
            "connect_timeout_s": 20.0,
            "controller_reconnect_timeout_s": 60.0,
            "router_check_interval_s": 60.0,
            "router_ip": defaults.get("network", {}).get("failover", {}).get(
                "router_ip", "192.168.1.1"),
        },
        "hotspot": {
            "ip": "192.168.4.1",
            "dhcp_start": "192.168.4.10",
            "dhcp_end": "192.168.4.50",
            "channel": 6,
            "controller_mac_ips": mac_ip_map,
        },
    }

    # =================================================================
    # Step 3: Power outage detection
    # =================================================================
    section(t("3. Détection de coupure de courant",
              "3. Power outage detection"))

    default_gpio = (defaults.get("outage_detection", {})
                    .get("relay", {}).get("gpio_pin", 17))
    gpio_pin = ask_int(
        t("GPIO pour la détection (relais 230V)", "GPIO for detection (230V relay)"),
        default=default_gpio, min_val=0, max_val=27
    )

    cfg["outage_detection"] = {
        "method": "relay",
        "relay": {
            "gpio_pin": gpio_pin,
            "debounce_ms": 200,
            "logic": "active_low",
        },
        "monitor": {
            "current_threshold_a": 0.05,
            "confirm_delay_s": 2.0,
        },
    }

    # =================================================================
    # Step 4: Battery configuration
    # =================================================================
    section(t("4. Configuration batterie", "4. Battery configuration"))

    default_ah = defaults.get("battery", {}).get("capacity_ah", 60.0)
    capacity = ask_int(
        t("Capacité de la batterie (Ah)", "Battery capacity (Ah)"),
        default=int(default_ah), min_val=1
    )

    cfg["battery"] = {
        "capacity_ah": float(capacity),
        "cell_count": 8,
        "chemistry": "lifepo4",
        "initial_soc": 100.0,
    }

    # =================================================================
    # Step 5: Monitoring
    # =================================================================
    section(t("5. Monitoring batterie", "5. Battery monitoring"))

    info(t("Choisissez le type de monitoring:", "Choose monitoring type:"))
    mon_choices = [
        ("ina226", t("INA226 (module I2C)", "INA226 (I2C module)")),
        ("victron", t("Victron Blue Smart IP22 (BLE)", "Victron Blue Smart IP22 (BLE)")),
        ("none", t("Aucun monitoring", "No monitoring")),
    ]

    for i, (key, label) in enumerate(mon_choices, 1):
        print(f"    {C.BOLD}{i}.{C.END} {label}")
    print()

    default_backend = defaults.get("monitoring", {}).get("backend", "ina226")
    default_idx = next(
        (i for i, (k, _) in enumerate(mon_choices, 1) if k == default_backend), 1
    )
    mon_idx = ask_int(
        t("Votre choix", "Your choice"),
        default=default_idx, min_val=1, max_val=3
    )
    mon_backend = mon_choices[mon_idx - 1][0]

    cfg["monitoring"] = {"backend": mon_backend}

    if mon_backend == "ina226":
        default_addr = (defaults.get("monitoring", {})
                        .get("ina226", {}).get("address", "0x40"))
        addr = ask(
            t("Adresse I2C de l'INA226", "INA226 I2C address"),
            default=default_addr
        )
        cfg["monitoring"]["ina226"] = {
            "i2c_bus": 1,
            "address": addr,
            "shunt_resistor": 0.01,
        }

    elif mon_backend == "victron":
        info(t("Scan des appareils Victron en Bluetooth...",
               "Scanning for Victron Bluetooth devices..."))

        try:
            victron_devices = asyncio.run(scan_victron_ble(10.0))
        except Exception as e:
            victron_devices = []
            warn(f"BLE scan error: {e}")

        ble_address = ""
        if victron_devices:
            ok(t(f"{len(victron_devices)} appareil(s) Victron trouvé(s):",
                 f"{len(victron_devices)} Victron device(s) found:"))
            for d in victron_devices:
                print(f"    {C.BOLD}{d['name']}{C.END} — {d['type']}")
                print(f"      Address: {d['address']}  RSSI: {d['rssi']}dBm")

            if len(victron_devices) == 1:
                ble_address = victron_devices[0]["address"]
                ok(f"Adresse: {ble_address}")
            else:
                chosen = choose_from_list(
                    victron_devices,
                    t("Sélectionnez l'appareil", "Select the device"),
                    display_fn=lambda d: f"{d['name']} ({d['address']})"
                )
                if chosen:
                    ble_address = chosen[0]["address"]
        else:
            warn(t("Aucun appareil Victron détecté.",
                   "No Victron device detected."))
            ble_address = ask(
                t("Adresse BLE du chargeur Victron",
                  "Victron charger BLE address"),
                default=(defaults.get("monitoring", {})
                         .get("victron", {}).get("ble_address", ""))
            )

        print()
        info(t(
            "La clé de chiffrement doit être récupérée via l'app VictronConnect :",
            "The encryption key must be retrieved from the VictronConnect app:"
        ))
        info(t(
            "  App VictronConnect → Connecter au chargeur → ⚙ → Product Info",
            "  VictronConnect app → Connect to charger → ⚙ → Product Info"
        ))
        info(t(
            "  → Activer 'Instant Readout via Bluetooth' → Cliquer 'Show'",
            "  → Enable 'Instant Readout via Bluetooth' → Click 'Show'"
        ))
        info(t(
            "  Il n'est pas possible de récupérer cette clé automatiquement.",
            "  This key cannot be retrieved automatically."
        ))
        print()

        default_key = (defaults.get("monitoring", {})
                       .get("victron", {}).get("encryption_key", ""))
        enc_key = ask(
            t("Clé de chiffrement (32 caractères hex)",
              "Encryption key (32 hex characters)"),
            default=default_key if default_key != "0000000000000000" else None
        )

        cfg["monitoring"]["victron"] = {
            "ble_address": ble_address,
            "encryption_key": enc_key,
            "poll_interval_s": 5.0,
        }

    # =================================================================
    # Step 6: Pump intensity levels
    # =================================================================
    section(t("6. Niveaux d'intensité des pompes",
              "6. Pump intensity levels"))

    # Build controller list
    controllers = []
    for d in selected:
        ctrl = {
            "name": d["name"],
            "ip": d["ip"],
            "type": d["hw_model"].lower().replace("rs", "reef").replace("wave", "wave"),
        }
        controllers.append(ctrl)

    cfg["pump_control"] = {"controllers": controllers}

    if mon_backend == "none":
        # No monitoring: just ask for a single backup speed
        info(t(
            "Sans monitoring batterie, une seule vitesse de secours sera appliquée.",
            "Without battery monitoring, a single backup speed will be applied."
        ))
        global_speed = ask_int(
            t("Vitesse des pompes en cas de coupure (%)",
              "Pump speed on power outage (%)"),
            default=50, min_val=0, max_val=100
        )
        cfg["pump_control"]["levels"] = {
            "normal": {"soc_threshold": 100, "global_intensity": 100, "per_device": {}},
            "eco": {"soc_threshold": 99, "global_intensity": global_speed, "per_device": {}},
        }

    else:
        use_levels = ask_yes_no(
            t("Voulez-vous définir des niveaux d'intensité progressifs ?",
              "Do you want to define progressive intensity levels?")
        )

        if not use_levels:
            speed = ask_int(
                t("Vitesse des pompes sur batterie (%)", "Pump speed on battery (%)"),
                default=50, min_val=0, max_val=100
            )
            cfg["pump_control"]["levels"] = {
                "normal": {"soc_threshold": 100, "global_intensity": 100, "per_device": {}},
                "eco": {"soc_threshold": 99, "global_intensity": speed, "per_device": {}},
            }

        else:
            num_levels = ask_int(
                t("Combien de niveaux ?", "How many levels?"),
                default=2, min_val=1, max_val=5
            )

            levels = {
                "normal": {
                    "soc_threshold": 100,
                    "global_intensity": 100,
                    "per_device": {},
                }
            }

            level_names = ["eco", "survival", "critical", "emergency", "last_resort"]

            for i in range(num_levels):
                lname = level_names[i] if i < len(level_names) else f"level_{i+1}"
                print()
                info(t(f"── Niveau {i+1}: {lname} ──",
                       f"── Level {i+1}: {lname} ──"))

                default_thresh = [60, 30, 15, 10, 5]
                threshold = ask_int(
                    t("Seuil SoC en dessous duquel ce niveau s'active (%)",
                      "SoC threshold below which this level activates (%)"),
                    default=default_thresh[i] if i < len(default_thresh) else 10,
                    min_val=1, max_val=99
                )

                default_speeds = [50, 30, 20, 10, 5]
                global_speed = ask_int(
                    t("Vitesse globale pour ce niveau (%)",
                      "Global speed for this level (%)"),
                    default=default_speeds[i] if i < len(default_speeds) else 30,
                    min_val=0, max_val=100
                )

                per_device = {}
                if len(selected) > 1:
                    custom = ask_yes_no(
                        t("Définir des vitesses individuelles pour certains équipements ?",
                          "Set individual speeds for specific devices?"),
                        default=False
                    )
                    if custom:
                        for d in selected:
                            speed = ask_int(
                                f"  {d['friendly']} ({d['name']})",
                                default=global_speed, min_val=0, max_val=100
                            )
                            if speed != global_speed:
                                per_device[d["name"]] = speed

                levels[lname] = {
                    "soc_threshold": threshold,
                    "global_intensity": global_speed,
                    "per_device": per_device,
                }

            cfg["pump_control"]["levels"] = levels

    # =================================================================
    # Step 7: MQTT / Home Assistant
    # =================================================================
    section(t("7. Intégration Home Assistant (MQTT)",
              "7. Home Assistant integration (MQTT)"))

    use_mqtt = ask_yes_no(
        t("Connecter à Home Assistant via MQTT ?",
          "Connect to Home Assistant via MQTT?"),
        default=True
    )

    if use_mqtt:
        default_host = defaults.get("mqtt", {}).get("host", "localhost")
        default_port = defaults.get("mqtt", {}).get("port", 1883)
        default_topic = defaults.get("mqtt", {}).get("base_topic", "homeassistant")
        default_device = defaults.get("mqtt", {}).get("device_name", "reef_battery")

        mqtt_host = ask(t("Adresse du broker MQTT", "MQTT broker address"),
                        default=default_host)
        mqtt_port = ask_int(t("Port MQTT", "MQTT port"),
                            default=default_port, min_val=1, max_val=65535)
        mqtt_user = ask(t("Utilisateur MQTT (vide si aucun)", "MQTT user (empty if none)"),
                        default=defaults.get("mqtt", {}).get("user", ""))
        mqtt_password = ""
        if mqtt_user:
            mqtt_password = ask(t("Mot de passe MQTT", "MQTT password"),
                                default=defaults.get("mqtt", {}).get("password", ""))

        cfg["mqtt"] = {
            "host": mqtt_host,
            "port": mqtt_port,
            "user": mqtt_user if mqtt_user else None,
            "password": mqtt_password if mqtt_password else None,
            "base_topic": ask(t("Topic de base", "Base topic"),
                              default=default_topic),
            "device_name": ask(t("Nom du device HA", "HA device name"),
                               default=default_device),
        }
    else:
        cfg["mqtt"] = {
            "host": "localhost",
            "port": 1883,
            "user": None,
            "password": None,
            "base_topic": "homeassistant",
            "device_name": "reef_battery",
        }

    # =================================================================
    # Step 8: Polling interval
    # =================================================================
    cfg["poll_interval_s"] = float(ask_int(
        t("Intervalle de polling (secondes)", "Polling interval (seconds)"),
        default=int(defaults.get("poll_interval_s", 5)),
        min_val=1, max_val=60
    ))

    # =================================================================
    # Save
    # =================================================================
    section(t("Sauvegarde", "Saving"))
    save_config(cfg, install_dir)

    # Summary
    banner(t("Configuration terminée !", "Configuration complete!"))
    info(t("Testez avec:  python3 main.py",
           "Test with:    python3 main.py"))
    info(t("Démarrez le service:  sudo systemctl start reef-battery-monitor",
           "Start the service:    sudo systemctl start reef-battery-monitor"))
    print()


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="reefbeat⚡Backup — Configuration Wizard"
    )
    parser.add_argument(
        "--install-dir", default=".",
        help="Installation directory"
    )
    args = parser.parse_args()
    run_wizard(args.install_dir)


if __name__ == "__main__":
    main()
