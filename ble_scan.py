#!/usr/bin/env python3
"""
BLE Scanner — Discover Victron devices and other BLE peripherals.

Scans for Bluetooth Low Energy devices with focus on finding
Victron Energy chargers/monitors. Displays address, name, RSSI,
and identifies Victron devices by their manufacturer data.

Usage:
  python3 ble_scan.py                  # General scan (10s)
  python3 ble_scan.py --duration 30    # Scan for 30 seconds
  python3 ble_scan.py --victron        # Show only Victron devices
  python3 ble_scan.py --verbose        # Show all advertisement data

Requirements:
  pip install bleak

On Raspberry Pi, ensure Bluetooth is enabled:
  sudo systemctl enable bluetooth
  sudo systemctl start bluetooth
  sudo hciconfig hci0 up
"""

import asyncio
import argparse
import sys
from datetime import datetime
from typing import Dict, Optional

try:
    from bleak import BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    print("ERROR: bleak not installed")
    print("Install with: pip install bleak")
    sys.exit(1)


# Victron Energy BLE manufacturer ID
VICTRON_MANUFACTURER_ID = 0x02E1  # 737 decimal

# Known Victron device types (from the BLE protocol)
VICTRON_DEVICE_TYPES = {
    0x01: "Solar Charger",
    0x02: "Battery Monitor",
    0x03: "Inverter",
    0x04: "DC/DC Charger",
    0x05: "SmartShunt",
    0x06: "Inverter/RS",
    0x07: "GX Device",
    0x08: "AC Charger",         # <-- Blue Smart IP22 is this
    0x09: "Smart Battery Protect",
    0x0A: "Lynx Smart BMS",
    0x0B: "Multi RS",
    0x0C: "VE.Bus",
    0x0D: "DC Energy Meter",
    0x0E: "Orion Smart",
}


class DeviceInfo:
    """Collected info about a discovered BLE device."""

    def __init__(self, device: BLEDevice, adv: AdvertisementData):
        self.address = device.address
        self.name = adv.local_name or device.name or "Unknown"
        self.rssi = adv.rssi
        self.is_victron = False
        self.victron_type: Optional[str] = None
        self.victron_type_id: Optional[int] = None
        self.manufacturer_data: Dict[int, bytes] = adv.manufacturer_data
        self.service_uuids = adv.service_uuids
        self.last_seen = datetime.now()
        self.seen_count = 1

        # Check if Victron device
        self._parse_victron(adv)

    def _parse_victron(self, adv: AdvertisementData):
        """Detect and parse Victron manufacturer data."""
        if VICTRON_MANUFACTURER_ID in adv.manufacturer_data:
            self.is_victron = True
            data = adv.manufacturer_data[VICTRON_MANUFACTURER_ID]
            if len(data) >= 2:
                # Byte 0: record type (should be 0x10 for product advertisement)
                # Byte 1: device type
                self.victron_type_id = data[1] if len(data) > 1 else None
                self.victron_type = VICTRON_DEVICE_TYPES.get(
                    self.victron_type_id, f"Unknown (0x{self.victron_type_id:02X})"
                )

    def update(self, device: BLEDevice, adv: AdvertisementData):
        """Update with fresh advertisement data."""
        self.rssi = adv.rssi
        self.name = adv.local_name or device.name or self.name
        self.last_seen = datetime.now()
        self.seen_count += 1
        self.manufacturer_data = adv.manufacturer_data

    def __str__(self):
        parts = [
            f"  Address : {self.address}",
            f"  Name    : {self.name}",
            f"  RSSI    : {self.rssi} dBm",
        ]
        if self.is_victron:
            parts.append(f"  Type    : Victron {self.victron_type}")
            if self.victron_type_id is not None:
                parts.append(f"  Type ID : 0x{self.victron_type_id:02X}")
        if self.seen_count > 1:
            parts.append(f"  Seen    : {self.seen_count}x")
        return "\n".join(parts)


async def scan(duration: float, victron_only: bool, verbose: bool):
    """Run BLE scan and collect results."""
    devices: Dict[str, DeviceInfo] = {}
    victron_count = 0

    def on_detection(device: BLEDevice, adv: AdvertisementData):
        nonlocal victron_count
        addr = device.address

        if addr in devices:
            devices[addr].update(device, adv)
        else:
            info = DeviceInfo(device, adv)
            devices[addr] = info
            if info.is_victron:
                victron_count += 1
                # Print immediately when Victron found
                print(f"\n{'='*50}")
                print(f"  ⚡ VICTRON DEVICE FOUND!")
                print(info)
                print(f"{'='*50}")

    print(f"Scanning for BLE devices ({duration}s)...")
    print(f"Mode: {'Victron only' if victron_only else 'All devices'}")
    print("-" * 50)

    scanner = BleakScanner(detection_callback=on_detection)
    await scanner.start()

    # Show countdown
    for remaining in range(int(duration), 0, -1):
        await asyncio.sleep(1)
        total = len(devices)
        sys.stdout.write(
            f"\r  {remaining}s remaining | "
            f"Found: {total} devices, {victron_count} Victron"
        )
        sys.stdout.flush()

    await scanner.stop()
    print("\n")

    return devices


def print_results(devices: Dict[str, DeviceInfo],
                  victron_only: bool, verbose: bool):
    """Display scan results."""

    # Separate Victron and other devices
    victron_devices = {
        k: v for k, v in devices.items() if v.is_victron
    }
    other_devices = {
        k: v for k, v in devices.items() if not v.is_victron
    }

    # Print Victron devices
    if victron_devices:
        print("=" * 60)
        print(f"  VICTRON DEVICES FOUND: {len(victron_devices)}")
        print("=" * 60)

        for addr, info in sorted(
                victron_devices.items(),
                key=lambda x: x[1].rssi, reverse=True):
            print()
            print(info)

            if verbose and info.manufacturer_data:
                for mid, data in info.manufacturer_data.items():
                    hex_data = data.hex()
                    print(f"  Mfr 0x{mid:04X}: {hex_data}")

        # Print config hint
        print()
        print("-" * 60)
        print("  CONFIGURATION HINT")
        print("-" * 60)
        print()
        print("  To use a Victron device with reef-battery, add to config.json:")
        print()

        for addr, info in victron_devices.items():
            print(f'    "victron": {{')
            print(f'        "ble_address": "{addr}",')
            print(f'        "encryption_key": "<GET FROM VICTRONCONNECT APP>"')
            print(f'    }}')
            print()
            print(f"  To get the encryption key for '{info.name}':")
            print(f"    1. Open VictronConnect app on your phone")
            print(f"    2. Connect to '{info.name}'")
            print(f"    3. Tap the gear icon -> Product Info")
            print(f"    4. Enable 'Instant Readout via Bluetooth'")
            print(f"    5. Tap 'Show' next to 'Instant Readout Details'")
            print(f"    6. Copy the encryption key")
            print()

    else:
        print("No Victron devices found.")
        print()
        print("Troubleshooting:")
        print("  - Is the Victron charger powered on?")
        print("  - Is Bluetooth enabled on the charger?")
        print("  - Is 'Instant Readout' enabled in VictronConnect?")
        print("  - Try increasing scan duration: --duration 30")
        print("  - Make sure no other device is actively connected")
        print("    to the charger via BLE (VictronConnect app)")

    # Print other devices (if not victron-only mode)
    if not victron_only and other_devices:
        print()
        print("-" * 60)
        print(f"  OTHER BLE DEVICES: {len(other_devices)}")
        print("-" * 60)

        # Sort by signal strength
        sorted_devices = sorted(
            other_devices.values(),
            key=lambda x: x.rssi, reverse=True
        )

        for info in sorted_devices:
            name = info.name
            if name == "Unknown" and not verbose:
                continue
            print(f"  {info.address}  {info.rssi:4d} dBm  {name}")

            if verbose and info.manufacturer_data:
                for mid, data in info.manufacturer_data.items():
                    print(f"    Mfr 0x{mid:04X}: {data.hex()[:40]}...")

    # Summary
    print()
    print("-" * 60)
    total = len(devices)
    named = sum(1 for d in devices.values() if d.name != "Unknown")
    print(f"  Total: {total} devices ({named} named, "
          f"{len(victron_devices)} Victron)")
    print("-" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Scan for BLE devices (Victron Energy focus)"
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=10.0,
        help="Scan duration in seconds (default: 10)"
    )
    parser.add_argument(
        "--victron", "-v", action="store_true",
        help="Show only Victron devices"
    )
    parser.add_argument(
        "--verbose", "-V", action="store_true",
        help="Show raw manufacturer data"
    )
    args = parser.parse_args()

    print()
    print("  🔵 Reef Battery — BLE Scanner")
    print()

    try:
        devices = asyncio.run(
            scan(args.duration, args.victron, args.verbose)
        )
        print_results(devices, args.victron, args.verbose)
    except KeyboardInterrupt:
        print("\nScan cancelled.")
    except Exception as e:
        print(f"\nError: {e}")
        print("\nMake sure Bluetooth is enabled:")
        print("  sudo systemctl start bluetooth")
        print("  sudo hciconfig hci0 up")
        sys.exit(1)


if __name__ == "__main__":
    main()
