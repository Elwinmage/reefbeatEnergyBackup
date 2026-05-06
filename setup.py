#!/usr/bin/env python3
"""
Reef Battery Monitor — Setup & Dependency Installer

Detects the platform (RPi 3, RPi Zero W, etc.), checks for required
system packages and Python modules, installs what's missing, and
validates the hardware (I2C, BLE, GPIO).

Usage:
  sudo python3 setup.py              # Full install
  sudo python3 setup.py --check      # Check only, don't install
  sudo python3 setup.py --minimal    # INA226 + relay only (no BLE)
"""

import subprocess
import sys
import os
import argparse
import shutil
from pathlib import Path


# =============================================================================
# Colors for terminal output
# =============================================================================

class C:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    INFO = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg):    print(f"  {C.OK}✓{C.END} {msg}")
def warn(msg):  print(f"  {C.WARN}⚠{C.END} {msg}")
def fail(msg):  print(f"  {C.FAIL}✗{C.END} {msg}")
def info(msg):  print(f"  {C.INFO}ℹ{C.END} {msg}")
def header(msg): print(f"\n{C.BOLD}{'='*60}\n  {msg}\n{'='*60}{C.END}")


# =============================================================================
# Platform detection
# =============================================================================

def detect_platform() -> dict:
    """Detect Raspberry Pi model and capabilities."""
    platform = {
        "model": "unknown",
        "has_wifi": False,
        "has_bluetooth": False,
        "has_gpio": False,
        "has_i2c": False,
        "is_rpi": False,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }

    # Check /proc/cpuinfo for RPi model
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        if "Raspberry Pi" in cpuinfo or "BCM" in cpuinfo:
            platform["is_rpi"] = True
            platform["has_gpio"] = True

            model_file = Path("/proc/device-tree/model")
            if model_file.exists():
                model = model_file.read_text().strip().rstrip("\x00")
                platform["model"] = model

                if "Zero W" in model or "Zero 2" in model:
                    platform["has_wifi"] = True
                    platform["has_bluetooth"] = True
                elif "3" in model or "4" in model or "5" in model:
                    platform["has_wifi"] = True
                    platform["has_bluetooth"] = True
                elif "Zero" in model:
                    # Zero v1.3 without W
                    platform["has_wifi"] = False
                    platform["has_bluetooth"] = False
    except Exception:
        pass

    # Check if I2C is enabled
    platform["has_i2c"] = Path("/dev/i2c-1").exists()

    # Check if Bluetooth adapter is present
    try:
        result = subprocess.run(
            ["hciconfig"], capture_output=True, text=True)
        platform["has_bluetooth"] = "hci0" in result.stdout
    except FileNotFoundError:
        pass

    # Check if Wi-Fi interface exists
    platform["has_wifi"] = Path("/sys/class/net/wlan0").exists()

    return platform


# =============================================================================
# System packages
# =============================================================================

APT_PACKAGES_CORE = [
    "python3-pip",
    "python3-dev",
    "i2c-tools",
]

APT_PACKAGES_HOTSPOT = [
    "hostapd",
    "dnsmasq",
    "wireless-tools",
]

APT_PACKAGES_BLUETOOTH = [
    "bluetooth",
    "bluez",
    "libglib2.0-dev",
]


def check_apt_package(pkg: str) -> bool:
    """Check if an apt package is installed."""
    result = subprocess.run(
        ["dpkg", "-s", pkg],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def install_apt_packages(packages: list, check_only: bool) -> bool:
    """Install apt packages if missing."""
    missing = [p for p in packages if not check_apt_package(p)]

    if not missing:
        ok("All system packages installed")
        return True

    if check_only:
        for pkg in missing:
            fail(f"Missing: {pkg}")
        return False

    info(f"Installing: {', '.join(missing)}")
    result = subprocess.run(
        ["sudo", "apt-get", "install", "-y"] + missing,
        capture_output=False,
    )
    if result.returncode != 0:
        fail("apt install failed")
        return False

    ok(f"Installed {len(missing)} packages")
    return True


# =============================================================================
# Python packages
# =============================================================================

PIP_PACKAGES_CORE = [
    "smbus2",
    "paho-mqtt",
    "requests",
]

PIP_PACKAGES_GPIO = [
    "RPi.GPIO",
]

PIP_PACKAGES_BLUETOOTH = [
    "bleak",
    "victron-ble",
]


def check_pip_package(pkg: str) -> bool:
    """Check if a Python package is importable."""
    import_name = pkg.lower().replace("-", "_").replace(".", "_")
    # Special cases
    special = {
        "paho_mqtt": "paho.mqtt.client",
        "rpi_gpio": "RPi.GPIO",
        "victron_ble": "victron_ble",
    }
    module = special.get(import_name, import_name)

    try:
        __import__(module)
        return True
    except ImportError:
        return False


def install_pip_packages(packages: list, check_only: bool) -> bool:
    """Install pip packages if missing."""
    missing = [p for p in packages if not check_pip_package(p)]

    if not missing:
        ok("All Python packages installed")
        return True

    if check_only:
        for pkg in missing:
            fail(f"Missing: {pkg}")
        return False

    info(f"Installing: {', '.join(missing)}")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "--break-system-packages"] + missing,
        capture_output=False,
    )
    if result.returncode != 0:
        fail("pip install failed")
        return False

    ok(f"Installed {len(missing)} Python packages")
    return True


# =============================================================================
# Hardware checks
# =============================================================================

def check_i2c() -> bool:
    """Check if I2C is enabled and detect devices."""
    if not Path("/dev/i2c-1").exists():
        fail("I2C not enabled")
        info("Enable with: sudo raspi-config -> Interfacing -> I2C")
        return False

    ok("I2C enabled (/dev/i2c-1)")

    # Scan for INA226
    try:
        result = subprocess.run(
            ["i2cdetect", "-y", "1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            output = result.stdout
            # Check for common INA226 addresses (0x40-0x4F)
            found = []
            for line in output.split("\n"):
                parts = line.split()
                for part in parts:
                    if part in ["40", "41", "44", "45"]:
                        found.append(f"0x{part}")
            if found:
                ok(f"I2C devices found at: {', '.join(found)}")
            else:
                warn("No INA226 detected (check wiring)")
                info("Expected at 0x40 (default address)")
    except Exception as e:
        warn(f"i2cdetect failed: {e}")

    return True


def check_bluetooth() -> bool:
    """Check Bluetooth adapter status."""
    try:
        result = subprocess.run(
            ["hciconfig", "hci0"],
            capture_output=True, text=True,
        )
        if "UP RUNNING" in result.stdout:
            ok("Bluetooth adapter UP and RUNNING")
            return True
        elif "DOWN" in result.stdout:
            warn("Bluetooth adapter DOWN")
            info("Enable with: sudo hciconfig hci0 up")
            return False
        else:
            fail("No Bluetooth adapter found")
            return False
    except FileNotFoundError:
        fail("hciconfig not found (install bluez)")
        return False


def check_gpio() -> bool:
    """Check if GPIO is accessible."""
    if Path("/dev/gpiomem").exists() or Path("/dev/mem").exists():
        ok("GPIO accessible")
        return True
    fail("GPIO not accessible")
    return False


# =============================================================================
# Service setup
# =============================================================================

def setup_systemd_service(check_only: bool) -> bool:
    """Install and configure the systemd service."""
    service_src = Path(__file__).parent / "reef-battery-monitor.service"
    service_dst = Path("/etc/systemd/system/reef-battery-monitor.service")

    if not service_src.exists():
        warn("Service file not found (reef-battery-monitor.service)")
        return False

    if check_only:
        if service_dst.exists():
            ok("Systemd service installed")
        else:
            warn("Systemd service not installed")
        return service_dst.exists()

    # Copy service file
    shutil.copy2(service_src, service_dst)
    ok("Service file installed")

    # Disable hotspot services (we manage them manually)
    for svc in ["hostapd", "dnsmasq"]:
        subprocess.run(
            ["sudo", "systemctl", "disable", svc],
            capture_output=True)
        subprocess.run(
            ["sudo", "systemctl", "stop", svc],
            capture_output=True)

    # Reload and enable
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)
    subprocess.run(
        ["sudo", "systemctl", "enable", "reef-battery-monitor"],
        capture_output=True)
    ok("Service enabled (will start on boot)")
    info("Start now with: sudo systemctl start reef-battery-monitor")
    info("View logs with: journalctl -u reef-battery-monitor -f")

    return True


# =============================================================================
# Config check
# =============================================================================

def check_config() -> bool:
    """Check if config.json exists and has been customized."""
    config_path = Path(__file__).parent / "config.json"

    if not config_path.exists():
        fail("config.json not found")
        info("Copy from config.json.example and edit")
        return False

    import json
    with open(config_path) as f:
        cfg = json.load(f)

    ok("config.json found")

    # Check for default values that need changing
    warnings = []

    ssid = cfg.get("network", {}).get("home_wifi", {}).get("ssid", "")
    if ssid == "YOUR_HOME_SSID" or not ssid:
        warnings.append("Wi-Fi SSID not configured (network.home_wifi.ssid)")

    password = cfg.get("network", {}).get("home_wifi", {}).get("password", "")
    if password == "YOUR_HOME_PASSWORD" or not password:
        warnings.append("Wi-Fi password not configured (network.home_wifi.password)")

    controllers = cfg.get("pump_control", {}).get("controllers", [])
    if not controllers:
        warnings.append("No pump controllers configured (pump_control.controllers)")

    backend = cfg.get("monitoring", {}).get("backend", "")
    if backend == "victron":
        ble_addr = cfg.get("monitoring", {}).get("victron", {}).get("ble_address", "")
        enc_key = cfg.get("monitoring", {}).get("victron", {}).get("encryption_key", "")
        if ble_addr == "AA:BB:CC:DD:EE:FF":
            warnings.append("Victron BLE address not configured")
            info("Run: python3 ble_scan.py --victron")
        if enc_key == "0000000000000000":
            warnings.append("Victron encryption key not configured")
            info("Get from VictronConnect: Settings -> Product Info -> Instant Readout")

    if warnings:
        for w in warnings:
            warn(w)
        info("Edit config.json before starting the service")
        return False

    ok("config.json looks properly configured")
    return True


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Reef Battery Monitor — Setup & Dependency Installer"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check only, don't install anything"
    )
    parser.add_argument(
        "--minimal", action="store_true",
        help="Minimal install (INA226 + relay, no BLE/hotspot)"
    )
    args = parser.parse_args()

    print()
    print(f"{C.BOLD}  🐠 Reef Battery Monitor — Setup{C.END}")
    print()

    # --- Platform ---
    header("Platform Detection")
    platform = detect_platform()
    info(f"Model     : {platform['model']}")
    info(f"Python    : {platform['python_version']}")
    info(f"Wi-Fi     : {'yes' if platform['has_wifi'] else 'no'}")
    info(f"Bluetooth : {'yes' if platform['has_bluetooth'] else 'no'}")
    info(f"GPIO      : {'yes' if platform['has_gpio'] else 'no'}")
    info(f"I2C       : {'yes' if platform['has_i2c'] else 'no'}")

    if not platform["is_rpi"]:
        warn("Not a Raspberry Pi — some features may not work")

    all_ok = True

    # --- System packages ---
    header("System Packages (apt)")

    if not args.check:
        info("Updating package list...")
        subprocess.run(
            ["sudo", "apt-get", "update", "-qq"],
            capture_output=True)

    if not install_apt_packages(APT_PACKAGES_CORE, args.check):
        all_ok = False

    if not args.minimal:
        if platform["has_wifi"]:
            if not install_apt_packages(APT_PACKAGES_HOTSPOT, args.check):
                all_ok = False
        else:
            warn("No Wi-Fi — skipping hotspot packages")

        if platform["has_bluetooth"]:
            if not install_apt_packages(APT_PACKAGES_BLUETOOTH, args.check):
                all_ok = False
        else:
            warn("No Bluetooth — skipping BLE packages")

    # --- Python packages ---
    header("Python Packages (pip)")

    if not install_pip_packages(PIP_PACKAGES_CORE, args.check):
        all_ok = False

    if platform["has_gpio"]:
        if not install_pip_packages(PIP_PACKAGES_GPIO, args.check):
            all_ok = False

    if not args.minimal and platform["has_bluetooth"]:
        if not install_pip_packages(PIP_PACKAGES_BLUETOOTH, args.check):
            all_ok = False

    # --- Hardware checks ---
    header("Hardware Checks")

    if not check_i2c():
        all_ok = False

    if platform["has_gpio"]:
        if not check_gpio():
            all_ok = False

    if not args.minimal and platform["has_bluetooth"]:
        if not check_bluetooth():
            all_ok = False

    # --- Config ---
    header("Configuration")
    check_config()  # Don't fail overall for config

    # --- Service ---
    header("Systemd Service")
    if not args.check:
        setup_systemd_service(args.check)
    else:
        setup_systemd_service(True)

    # --- Summary ---
    header("Summary")
    if all_ok:
        ok("All dependencies installed and hardware detected")
        print()
        info("Next steps:")
        info("  1. Edit config.json with your settings")
        if not args.minimal:
            info("  2. Run: python3 ble_scan.py --victron  (if using Victron)")
        info("  3. Test: python3 main.py")
        info("  4. Start service: sudo systemctl start reef-battery-monitor")
    else:
        warn("Some issues detected — review the output above")
        if args.check:
            info("Run without --check to install missing dependencies")

    print()


if __name__ == "__main__":
    main()
