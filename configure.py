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
# rfkill helpers
# =============================================================================

def rfkill_status() -> Dict[str, Dict[str, bool]]:
    """
    Return the rfkill state for each radio type.

    Output: {"wifi": {"soft": bool, "hard": bool}, "bluetooth": {...}, ...}
    Missing entries simply mean the radio is not present on this host.
    """
    state: Dict[str, Dict[str, bool]] = {}
    try:
        # -J gives JSON output; available on util-linux >= 2.31 (RPi OS ships it)
        result = subprocess.run(
            ["rfkill", "--json"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return state
        data = json.loads(result.stdout or "{}")
        # Newer rfkill returns {"rfkilldevices": [...]}, older {"": [...]}
        entries = (
            data.get("rfkilldevices")
            or data.get("")
            or next(iter(data.values()), [])
            if data else []
        )
        for entry in entries:
            rtype = entry.get("type", "").lower()
            if not rtype:
                continue
            state[rtype] = {
                "soft": entry.get("soft", "unblocked") == "blocked",
                "hard": entry.get("hard", "unblocked") == "blocked",
            }
    except FileNotFoundError:
        # rfkill binary not installed
        pass
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        pass
    return state


def rfkill_unblock(radio: str) -> bool:
    """Soft-unblock a radio type ('wifi' or 'bluetooth'). Returns True on success."""
    try:
        result = subprocess.run(
            ["rfkill", "unblock", radio],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def ensure_radios_unblocked(check_bluetooth: bool = False) -> None:
    """
    Ensure Wi-Fi (and optionally Bluetooth) are not soft-blocked by rfkill.

    Hard blocks come from physical switches and can only be reported, not fixed.
    Requires sudo for the unblock action; if not root, we attempt anyway and
    fall back to a clear warning.
    """
    radios_to_check = ["wifi"]
    if check_bluetooth:
        radios_to_check.append("bluetooth")

    state = rfkill_status()
    if not state:
        # rfkill not available -- nothing we can sensibly do, stay silent
        return

    for radio in radios_to_check:
        # rfkill reports wifi under either "wlan" or "wifi" depending on version
        entry = state.get(radio) or state.get("wlan" if radio == "wifi" else radio)
        label = "Wi-Fi" if radio == "wifi" else "Bluetooth"

        if entry is None:
            warn(t(f"{label} non détecté par rfkill", f"{label} not detected by rfkill"))
            continue

        if entry["hard"]:
            # Hard block: physical switch, cannot fix from software
            fail(t(
                f"{label} bloqué matériellement (interrupteur physique). "
                "Veuillez l'activer manuellement.",
                f"{label} hard-blocked (physical switch). Please enable it manually."
            ))
            continue

        if entry["soft"]:
            info(t(f"{label} bloqué (soft) par rfkill, déblocage...",
                   f"{label} soft-blocked by rfkill, unblocking..."))
            if rfkill_unblock(radio):
                ok(t(f"{label} débloqué", f"{label} unblocked"))
            else:
                fail(t(
                    f"Impossible de débloquer {label}. "
                    "Relancez en root: sudo rfkill unblock " + radio,
                    f"Cannot unblock {label}. Rerun as root: sudo rfkill unblock " + radio
                ))
        else:
            ok(t(f"{label} actif (rfkill OK)", f"{label} active (rfkill OK)"))


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

# ReefBeat device types we care about for energy backup.
# Note: RSLED (ReefLED) intentionally excluded — energy backup focuses on
# circulation pumps (RSWAVE) and return/skimmer pumps (RSRUN). Lighting is
# considered acceptable to lose during a power outage.
BACKUP_DEVICE_TYPES = {
    "RSWAVE45": "ReefWave 45",
    "RSWAVE25": "ReefWave 25",
    "RSRUN":    "ReefRun",
}

# All known ReefBeat hw_model IDs (for detection)
ALL_REEFBEAT_MODELS = {
    "RSWAVE45", "RSWAVE25",
    "RSRUN",
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


def get_reefbeat_dashboard(ip: str) -> Optional[Dict]:
    """
    Get the /dashboard JSON from a ReefBeat device.

    Multi-pump devices (RSRUN) expose pump_1/pump_2 sub-objects with
    individual 'name', 'type' and 'model' fields. RSWAVE has its own
    flatter structure (and exposes "/" rather than "/dashboard" for
    device-info — this helper is only used for RSRUN here).
    """
    if not HAS_REQUESTS:
        return None
    try:
        r = req_lib.get(f"http://{ip}/dashboard", timeout=3)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# Per-family pump intensity ranges.
# Floor = lowest non-zero value the pump accepts. 0 always means OFF.
PUMP_INTENSITY_RANGES = {
    "RSWAVE45": {"min_running": 10, "max": 100},
    "RSWAVE25": {"min_running": 10, "max": 100},
    "RSRUN":    {"min_running": 40, "max": 100},
}


def get_intensity_range(hw_model: str) -> Tuple[int, int]:
    """
    Return (min_running, max) for a given hw_model.
    A value of 0 is always allowed (=OFF) regardless of min_running.
    """
    rng = PUMP_INTENSITY_RANGES.get(hw_model, {"min_running": 0, "max": 100})
    return rng["min_running"], rng["max"]


def ask_pump_intensity(prompt: str, hw_model: str, default: int = None) -> int:
    """
    Ask for a pump intensity, enforcing per-model bounds.

    Rules:
      - 0 is always allowed (means OFF)
      - otherwise must be within [min_running, max]
    """
    min_running, max_val = get_intensity_range(hw_model)

    # Build a hint string for the user
    if min_running > 0:
        hint = t(
            f"0 = OFF, sinon {min_running}–{max_val}",
            f"0 = OFF, otherwise {min_running}–{max_val}"
        )
    else:
        hint = t(f"0–{max_val}", f"0–{max_val}")

    full_prompt = f"{prompt} ({hint})"

    while True:
        val = ask(full_prompt, default)
        try:
            n = int(val)
        except ValueError:
            warn(t("Entrez un nombre entier", "Enter an integer"))
            continue

        if n == 0:
            return 0
        if n < min_running:
            warn(t(
                f"Valeur trop basse pour ce modèle. Minimum en marche: {min_running}% (ou 0 pour OFF)",
                f"Value too low for this model. Running minimum: {min_running}% (or 0 for OFF)"
            ))
            continue
        if n > max_val:
            warn(t(f"Maximum: {max_val}", f"Maximum: {max_val}"))
            continue
        return n


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


def pump_key(d: Dict) -> str:
    """Stable per-pump identifier used as the key in PumpController.

    For RSWAVE (one pump per device), use the device name. For RSRUN
    (two pumps per device), append the pump index so each pump gets a
    distinct entry in the level mapping.
    """
    if d.get("pump_index"):
        return f"{d['name']}::{d['pump_index']}"
    return d["name"]


def run_wizard(install_dir: str):
    """Run the interactive configuration wizard."""
    banner("reefbeat⚡Backup — Configuration")
    defaults = load_existing_config(install_dir)
    cfg: Dict[str, Any] = {}

    # =================================================================
    # Step 0: rfkill — make sure radios are not blocked
    # =================================================================
    # Wi-Fi is always required (we scan + connect to ReefBeat devices).
    # Bluetooth is only required if the user later picks Victron monitoring,
    # but unblocking it preventively costs nothing and avoids a re-run.
    section(t("0. Vérification des radios (rfkill)",
              "0. Radio check (rfkill)"))
    ensure_radios_unblocked(check_bluetooth=True)

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
    # Step 1b: Expand multi-pump devices (RSRUN) into individual entries
    # =================================================================
    # An RSRUN exposes /dashboard with pump_1 / pump_2 sub-objects, each with
    # its own name / type / model. We unfold these so the rest of the wizard
    # treats each physical pump as a distinct controllable item.
    # Single-pump devices (RSWAVE, RSLED) stay as-is.
    expanded: List[Dict] = []
    for d in selected:
        hw = d["hw_model"]
        if hw.startswith("RSRUN"):
            dash = get_reefbeat_dashboard(d["ip"])
            pumps_found = []
            if dash:
                for key in ("pump_1", "pump_2", "pump_3", "pump_4"):
                    p = dash.get(key)
                    if isinstance(p, dict) and not p.get("missing_pump", False):
                        pumps_found.append((key, p))

            if not pumps_found:
                # Could not enumerate pumps -> fall back to a single entry
                warn(t(
                    f"{d['name']}: dashboard indisponible, traité comme pompe unique",
                    f"{d['name']}: dashboard unavailable, treated as single pump"
                ))
                expanded.append({**d, "pump_index": None,
                                 "pump_type": None, "pump_model": None,
                                 "display_name": d["name"]})
                continue

            for key, p in pumps_found:
                pump_name = p.get("name", f"{d['name']} {key}")
                pump_type = p.get("type", "")
                pump_model = p.get("model", "")
                display = f"{d['name']} / {pump_name}"
                if pump_type or pump_model:
                    display += f" [{pump_type or pump_model}]"
                expanded.append({
                    **d,
                    "pump_index": key,            # "pump_1" / "pump_2"
                    "pump_type": pump_type,       # "return" / "skimmer" / ...
                    "pump_model": pump_model,     # "return-12000" / "rsk-900"
                    "pump_name": pump_name,
                    "display_name": display,
                })
                ok(f"  {display} ({d['ip']})")
        else:
            # Single-pump devices (RSWAVE, RSLED): keep as-is
            expanded.append({**d, "pump_index": None,
                             "pump_type": None, "pump_model": None,
                             "display_name": d["name"]})

    selected = expanded
    ok(t(f"→ {len(selected)} pompe(s) à piloter",
         f"→ {len(selected)} pump(s) to control"))

    # =================================================================
    # Step 2: Wi-Fi configuration
    # =================================================================
    section(t("2. Configuration Wi-Fi", "2. Wi-Fi configuration"))

    # Get SSID from ReefBeat devices
    # Note: multi-pump devices (RSRUN) appear several times in `selected`
    # but share a single IP — dedupe to avoid querying the same box twice.
    device_ssids = {}
    device_macs = {}
    seen_ips: set = set()

    for d in selected:
        ip = d["ip"]
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
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
    # INA226 is now the mandatory primary battery monitor (it sees the
    # actual battery current at all times, including during outages).
    # Victron BLE is an optional auxiliary that adds charger-side
    # telemetry (state, output current/voltage) on top.
    section(t("5. Monitoring batterie", "5. Battery monitoring"))

    info(t(
        "INA226 (shunt I2C) est le monitoring principal et obligatoire.",
        "INA226 (I2C shunt) is the mandatory primary battery monitor."
    ))
    info(t(
        "Il mesure le courant batterie réel en permanence, même secteur coupé.",
        "It measures the real battery current at all times, even on outage."
    ))
    print()

    # --- I2C sanity check ---
    # The INA226 module is mandatory, so we surface a clear warning if the
    # bus isn't available. We don't block the wizard on this -- the user
    # might be configuring this on a different machine -- but they should
    # know early.
    if not os.path.exists("/dev/i2c-1"):
        warn(t(
            "/dev/i2c-1 absent : le bus I2C ne semble pas activé sur ce Pi.",
            "/dev/i2c-1 missing: the I2C bus does not appear to be enabled."
        ))
        info(t(
            "Activez-le avec : sudo raspi-config nonint do_i2c 0",
            "Enable it with: sudo raspi-config nonint do_i2c 0"
        ))
        info(t("puis redémarrez le Pi avant de lancer le service.",
               "then reboot the Pi before starting the service."))
    else:
        # Try a quick i2cdetect to spot the INA226. Non-fatal.
        try:
            out = subprocess.run(
                ["i2cdetect", "-y", "1"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0:
                # Look for any of the four common INA226 addresses.
                hits = [a for a in ("40", "41", "44", "45")
                        if f" {a} " in out.stdout]
                if hits:
                    ok(t(
                        f"Composant I2C détecté à 0x{hits[0]} (probable INA226).",
                        f"I2C device detected at 0x{hits[0]} (probably the INA226)."
                    ))
                else:
                    warn(t(
                        "Aucun composant détecté à 0x40/0x41/0x44/0x45.",
                        "No device detected at 0x40/0x41/0x44/0x45."
                    ))
                    info(t("Vérifiez le câblage SDA/SCL/3V3/GND.",
                           "Check the SDA/SCL/3V3/GND wiring."))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # i2cdetect is part of i2c-tools; install.sh installs it but
            # the user may have skipped it. Don't make this fatal.
            pass
    print()

    default_addr = (defaults.get("monitoring", {})
                    .get("ina226", {}).get("address", "0x40"))
    default_shunt = (defaults.get("monitoring", {})
                     .get("ina226", {}).get("shunt_resistor", 0.002))
    addr = ask(
        t("Adresse I2C de l'INA226", "INA226 I2C address"),
        default=default_addr
    )
    # Shunt resistor value: ALWAYS required (used to compute current from
    # the shunt voltage drop). Common values for ready-made INA226 modules:
    #   - 0.002 Ω: most "20A / 36V" modules (Amazon/Aliexpress generic)
    #   - 0.01  Ω: some "100A" modules (less common)
    #   - 0.1   Ω: low-current breakouts (Adafruit, ~1A max)
    # The user must confirm the value printed on their module's shunt
    # resistor (look for R002, R010, R100 markings).
    info(t(
        "Valeurs typiques : 0.002 Ω pour les modules 20A (génériques),",
        "Typical values: 0.002 Ω for 20A modules (generic),"
    ))
    info(t(
        "                  0.01 Ω pour les modules 100A,",
        "                 0.01 Ω for 100A modules,"
    ))
    info(t(
        "                  0.1 Ω pour les modules basse intensité (~1A).",
        "                 0.1 Ω for low-current breakouts (~1A)."
    ))
    while True:
        raw = ask(
            t("Valeur du shunt (Ohms)", "Shunt resistor value (Ohms)"),
            default=str(default_shunt)
        )
        try:
            shunt = float(raw)
        except ValueError:
            warn(t("Valeur invalide", "Invalid value"))
            continue
        if shunt <= 0 or shunt > 1.0:
            warn(t("Doit être entre 0 et 1 Ohm",
                   "Must be between 0 and 1 Ohm"))
            continue
        break
    cfg["monitoring"] = {
        "ina226": {
            "i2c_bus": 1,
            "address": addr,
            "shunt_resistor": shunt,
        },
    }
    # Backwards-compatible flag for components that still inspect it.
    # New code should look at the presence of the "ina226" / "victron"
    # subkeys directly.
    cfg["monitoring"]["backend"] = "ina226"

    # --- Optional Victron auxiliary ---
    print()
    info(t(
        "Optionnel : ajouter un chargeur Victron Blue Smart (BLE) pour publier",
        "Optional: add a Victron Blue Smart charger (BLE) to publish"
    ))
    info(t(
        "son état (mode bulk/absorption/storage, courant de sortie) dans HA.",
        "its state (bulk/absorption/storage mode, output current) to HA."
    ))
    add_victron = ask_yes_no(
        t("Ajouter un chargeur Victron en complément ?",
          "Add a Victron charger as a complement?"),
        default=bool(defaults.get("monitoring", {}).get("victron")),
    )

    if add_victron:
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
        }
    # If the user declines, we simply omit the "victron" subkey -- the
    # factory in monitor.py won't instantiate the auxiliary backend.

    # =================================================================
    # Step 6: Pump intensity levels
    # =================================================================
    section(t("6. Niveaux d'intensité des pompes",
              "6. Pump intensity levels"))

    # Build a unique key per controllable pump. For multi-pump devices
    # (RSRUN) the parent `name` is the same for both pumps, so we suffix
    # with the pump_index ("pump_1"/"pump_2"). For single-pump devices
    # we keep the device name as-is.
    # Build controller list (one entry per controllable pump)
    controllers = []
    for d in selected:
        ctrl = {
            "key": pump_key(d),
            "name": d["name"],
            "ip": d["ip"],
            "hw_model": d["hw_model"],
            "type": d["hw_model"].lower().replace("rs", "reef"),
        }
        # Multi-pump RSRUN: include pump-specific addressing
        if d.get("pump_index"):
            ctrl["pump_index"] = d["pump_index"]      # "pump_1" / "pump_2"
            ctrl["pump_type"] = d.get("pump_type")    # "return" / "skimmer"
            ctrl["pump_model"] = d.get("pump_model")  # "return-12000" / "rsk-900"
            ctrl["pump_name"] = d.get("pump_name")
        controllers.append(ctrl)

    cfg["pump_control"] = {"controllers": controllers}

    # Compute the global lower bound: the global intensity of any level
    # must be valid for every selected pump. So the floor is the highest
    # min_running among all present models. 0 (=OFF) is always allowed.
    present_models = {d["hw_model"] for d in selected}
    global_min_running = max(
        (get_intensity_range(m)[0] for m in present_models), default=0
    )
    global_max = min(
        (get_intensity_range(m)[1] for m in present_models), default=100
    )

    def ask_global_intensity(prompt: str, default: int) -> int:
        """
        Ask for a level-wide intensity. Must be either 0 (OFF) or within
        [global_min_running, global_max]. Per-device overrides can later
        loosen this for individual pumps.
        """
        if global_min_running > 0:
            hint = t(
                f"0 = tout OFF, sinon {global_min_running}–{global_max}",
                f"0 = all OFF, otherwise {global_min_running}–{global_max}"
            )
        else:
            hint = f"0–{global_max}"
        full = f"{prompt} ({hint})"
        while True:
            val = ask(full, default)
            try:
                n = int(val)
            except ValueError:
                warn(t("Entrez un nombre entier", "Enter an integer"))
                continue
            if n == 0:
                return 0
            if n < global_min_running:
                warn(t(
                    f"Trop bas pour les modèles présents (min commun: {global_min_running}%, "
                    "ou 0 pour OFF). Utilisez les valeurs par équipement pour aller plus bas.",
                    f"Too low for the present models (common min: {global_min_running}%, "
                    "or 0 for OFF). Use per-device values to go lower."
                ))
                continue
            if n > global_max:
                warn(f"Maximum: {global_max}")
                continue
            return n

    # =================================================================
    # Backup mode: auto (target-driven) / single speed
    # =================================================================
    # The auto path uses power_estimation to compute SoC thresholds and
    # per-device intensities from a stated autonomy target. For users
    # who want a single backup speed regardless of SoC, "simple" gives
    # exactly one degraded level.
    print()
    info(t("Deux façons de configurer le mode batterie :",
           "Two ways to configure backup behaviour:"))
    mode_choices = [
        ("auto", t("Auto : je donne une cible d'autonomie, le wizard calcule",
                   "Auto: I state a target autonomy, the wizard computes")),
        ("simple", t("Simple : une seule vitesse de secours",
                     "Simple: a single backup speed")),
    ]
    for i, (k, label) in enumerate(mode_choices, 1):
        print(f"    {C.BOLD}{i}.{C.END} {label}")
    print()
    mode_idx = ask_int(
        t("Votre choix", "Your choice"),
        default=1, min_val=1, max_val=2
    )
    backup_mode = mode_choices[mode_idx - 1][0]

    if backup_mode == "auto":
        cfg["pump_control"]["levels"] = _build_auto_scenario(
            cfg, selected, defaults
        )
    else:
        speed = ask_global_intensity(
            t("Vitesse des pompes sur batterie (%)",
              "Pump speed on battery (%)"),
            default=max(50, global_min_running)
        )
        cfg["pump_control"]["levels"] = {
            "normal": {"soc_threshold": 100, "global_intensity": 100, "per_device": {}},
            "eco": {"soc_threshold": 99, "global_intensity": speed, "per_device": {}},
        }

    # =================================================================
    # Step 7 & 8: MQTT + Polling + Save
    # =================================================================
    _step7_mqtt(cfg, defaults)
    _step8_polling_and_save(cfg, defaults, install_dir)


def _build_auto_scenario(cfg: dict, selected: list, defaults: dict) -> dict:
    """
    Interactive auto-scenario path.

    Asks for the autonomy target and any auxiliary loads, then uses the
    power_estimation module to produce a level set sized for the user's
    actual hardware and battery.
    """
    from power_estimation import (
        DeviceSpec, build_scenario, format_scenario,
        detect_raspberry_pi,
    )

    # Battery capacity (already in cfg from step 4)
    capacity_ah = cfg.get("battery", {}).get("capacity_ah", 60.0)
    # 24V LiFePO4 nominal, depth of discharge 80% (standard safe value).
    # Anything more aggressive risks shortening the cycle life.
    nominal_v = 25.6
    dod = 0.8
    capacity_wh = capacity_ah * nominal_v * dod

    info(t(
        f"Capacité utile estimée : {capacity_wh:.0f} Wh "
        f"(={capacity_ah:.0f} Ah × 25.6 V × 80% DoD)",
        f"Estimated usable capacity: {capacity_wh:.0f} Wh "
        f"(={capacity_ah:.0f} Ah × 25.6 V × 80% DoD)"
    ))
    print()

    # Auxiliary load: try to auto-detect the Pi, then ask for any extras.
    pi_label, pi_w = detect_raspberry_pi()
    if pi_label and pi_w > 0:
        ok(t(f"Raspberry Pi détecté : {pi_label} (~{pi_w:.1f} W)",
             f"Raspberry Pi detected: {pi_label} (~{pi_w:.1f} W)"))
    elif pi_label:
        warn(t(f"Pi détecté ({pi_label}) mais conso inconnue.",
               f"Pi detected ({pi_label}) but power unknown."))
        pi_w = 4.0
    else:
        pi_w = 4.0  # generic fallback
        info(t("Pi non détecté, estimation 4 W par défaut.",
               "Pi not detected, defaulting to 4 W."))

    add_extra = ask_yes_no(
        t("Avez-vous d'autres équipements alimentés sur la batterie "
          "(éclairage de secours, capteurs, switchs réseau...) ?",
          "Do you have other equipment powered by the battery "
          "(emergency lighting, sensors, network switches...)?"),
        default=False,
    )
    extra_w = 0.0
    if add_extra:
        while True:
            raw = ask(
                t("Conso totale supplémentaire en watts (somme)",
                  "Total extra power draw in watts (sum)"),
                default="10",
            )
            try:
                extra_w = float(raw)
                if extra_w >= 0:
                    break
            except ValueError:
                pass
            warn(t("Valeur invalide", "Invalid value"))

    aux_load_w = pi_w + extra_w

    # Target autonomy
    while True:
        raw = ask(
            t("Cible d'autonomie en heures",
              "Target autonomy in hours"),
            default="12",
        )
        try:
            target_h = float(raw)
            if 1.0 <= target_h <= 72.0:
                break
        except ValueError:
            pass
        warn(t("Valeur invalide (1 à 72)", "Invalid value (1 to 72)"))

    # Build DeviceSpec list from the selected equipment
    devices = []
    for d in selected:
        hw = d["hw_model"]
        if hw.startswith("RSWAVE"):
            family, role, floor = "wave", "wave", 10
            # ReefWave model name from BACKUP_DEVICE_TYPES
            pump_model = BACKUP_DEVICE_TYPES.get(hw, "ReefWave 45")
        elif hw.startswith("RSRUN"):
            family, floor = "run", 40
            role = d.get("pump_type") or "return"
            pump_model = d.get("pump_model") or "return-12000"
        else:
            continue
        devices.append(DeviceSpec(
            key=pump_key(d),
            family=family,
            role=role,
            pump_model=pump_model,
            floor_pct=floor,
        ))

    levels = build_scenario(
        target_h=target_h,
        capacity_wh=capacity_wh,
        devices=devices,
        aux_load_w=aux_load_w,
    )

    # Show the proposed plan and ask for confirmation
    print()
    print(format_scenario(levels, target_h=target_h, capacity_wh=capacity_wh))
    print()

    if not ask_yes_no(
        t("Accepter ce plan ?", "Accept this plan?"),
        default=True,
    ):
        info(t(
            "Plan rejeté -- relancez le wizard pour ajuster ou choisir "
            "le mode manuel.",
            "Plan rejected -- re-run the wizard to adjust or pick "
            "manual mode."
        ))
        sys.exit(0)

    # Convert ScenarioLevel objects into the format expected by
    # PumpController. We compute global_intensity as the most common
    # value among per-device entries (defaults to 100 for "normal").
    out: dict = {}
    for lvl in levels:
        # Prefer the wave intensity as the global default; per_device
        # overrides cover everything else.
        wave_intensities = [
            v for k, v in lvl.per_device.items()
            if any(d.key == k and d.role == "wave" for d in devices)
        ]
        if lvl.name == "normal":
            global_int = 100
        elif wave_intensities:
            global_int = wave_intensities[0]
        else:
            global_int = max(lvl.per_device.values()) if lvl.per_device else 0

        # Per-device only keeps overrides that differ from the global
        per_device = {
            k: v for k, v in lvl.per_device.items() if v != global_int
        }
        out[lvl.name] = {
            "soc_threshold": lvl.soc_threshold,
            "global_intensity": global_int,
            "per_device": per_device,
        }
    return out


def _step7_mqtt(cfg: dict, defaults: dict):
    """Step 7: MQTT / Home Assistant configuration."""
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

        # User/password are mandatory when MQTT is enabled. We loop until
        # both fields are non-empty so the wizard can't produce a config
        # that will silently fail authentication at runtime.
        default_user = defaults.get("mqtt", {}).get("user", "") or ""
        while True:
            mqtt_user = ask(t("Utilisateur MQTT", "MQTT user"),
                            default=default_user if default_user else None)
            if mqtt_user.strip():
                break
            warn(t("L'utilisateur MQTT est obligatoire.",
                   "MQTT user is required."))

        default_pw = defaults.get("mqtt", {}).get("password", "") or ""
        while True:
            mqtt_password = ask(t("Mot de passe MQTT", "MQTT password"),
                                default=default_pw if default_pw else None)
            if mqtt_password.strip():
                break
            warn(t("Le mot de passe MQTT est obligatoire.",
                   "MQTT password is required."))

        cfg["mqtt"] = {
            "host": mqtt_host,
            "port": mqtt_port,
            "user": mqtt_user,
            "password": mqtt_password,
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


def _step8_polling_and_save(cfg: dict, defaults: dict, install_dir: str):
    """Step 8: Polling interval + save configuration."""
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
