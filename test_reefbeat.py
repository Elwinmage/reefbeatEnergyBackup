#!/usr/bin/env python3
"""
reefbeat⚡Backup — ReefBeat Equipment Test CLI

Tests communication with ReefBeat devices to verify that the backup
system can read state, change pump intensity, and restore the original
configuration.

Usage:
  python3 test_reefbeat.py                    # Interactive guided test
  python3 test_reefbeat.py --list             # List configured devices
  python3 test_reefbeat.py --read             # Read current state of all devices
  python3 test_reefbeat.py --test-all         # Full test cycle on all devices
  python3 test_reefbeat.py --test PUMP_KEY    # Test a specific pump
  python3 test_reefbeat.py --dry-run          # Show what would be done (no changes)
  python3 test_reefbeat.py --config /path/to/config.json

Test cycle per device:
  1. Read current state (schedule/wave program + intensity)
  2. Save snapshot
  3. Apply test intensity (50% for 5 seconds)
  4. Read new state (verify change applied)
  5. Restore original configuration from snapshot
  6. Read final state (verify restoration matches original)
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# =============================================================================
# Terminal helpers
# =============================================================================

class C:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    INFO = "\033[94m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    END = "\033[0m"

def ok(msg):    print(f"  {C.OK}✓{C.END} {msg}")
def warn(msg):  print(f"  {C.WARN}⚠{C.END} {msg}")
def fail(msg):  print(f"  {C.FAIL}✗{C.END} {msg}")
def info(msg):  print(f"  {C.INFO}ℹ{C.END} {msg}")
def header(msg): print(f"\n{C.BOLD}{'='*60}\n  {msg}\n{'='*60}{C.END}")
def section(msg): print(f"\n{C.BOLD}{C.INFO}── {msg} ──{C.END}\n")


# =============================================================================
# HTTP helpers (standalone, no dependency on controller.py)
# =============================================================================

DEBUG = False

def dbg(msg):
    """Print debug message if DEBUG mode is enabled."""
    if DEBUG:
        print(f"  {C.DIM}[DBG] {msg}{C.END}")

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)


def http_get(ip: str, path: str) -> Optional[Any]:
    """GET from a ReefBeat device, returns parsed JSON or None."""
    url = f"http://{ip}{path}"
    dbg(f"→ GET {url}")
    try:
        r = requests.get(url, timeout=3)
        dbg(f"← {r.status_code} ({len(r.text)} bytes)")
        if DEBUG and r.ok:
            # Truncate long responses
            body = r.text[:500]
            if len(r.text) > 500:
                body += f"... ({len(r.text)} total)"
            dbg(f"← {body}")
        if r.ok:
            return r.json()
        warn(f"GET {ip}{path} → HTTP {r.status_code}")
        if DEBUG:
            dbg(f"← body: {r.text[:300]}")
    except requests.exceptions.RequestException as e:
        fail(f"GET {ip}{path} → {type(e).__name__}")
        dbg(f"← exception: {e}")
    return None


def http_send(ip: str, path: str, payload: Any = "",
              method: str = "put") -> bool:
    """Send PUT/POST/DELETE to a ReefBeat device."""
    url = f"http://{ip}{path}"
    dbg(f"→ {method.upper()} {url}")
    if payload and payload != "":
        payload_str = json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else str(payload)
        dbg(f"→ payload: {payload_str[:500]}")
    try:
        if method == "put":
            r = requests.put(url, json=payload, timeout=5)
        elif method == "post":
            if payload and payload != "":
                r = requests.post(url, json=payload, timeout=5)
            else:
                r = requests.post(url, timeout=5)
        elif method == "delete":
            r = requests.delete(url, timeout=5)
        else:
            return False
        dbg(f"← {r.status_code}")
        if DEBUG and not r.ok:
            dbg(f"← body: {r.text[:300]}")
        if not r.ok:
            warn(f"{method.upper()} {url} → HTTP {r.status_code}")
        return r.ok
    except requests.exceptions.RequestException as e:
        fail(f"{method.upper()} {url} → {type(e).__name__}")
        dbg(f"← exception: {e}")
        return False


# =============================================================================
# Device state reading
# =============================================================================

def read_device_state(ctrl: dict) -> Optional[Dict]:
    """Read the complete state of a device/pump."""
    ip = ctrl["ip"]
    hw = ctrl["hw_model"]
    state = {"ip": ip, "hw_model": hw, "key": ctrl["key"]}

    # Device info
    device_info = http_get(ip, "/device-info")
    if device_info:
        state["name"] = device_info.get("name", "?")
        state["fw_version"] = device_info.get("fw_version", "?")
    else:
        state["name"] = ctrl.get("name", "?")
        state["reachable"] = False
        return state

    state["reachable"] = True

    # Mode (on/off)
    mode = http_get(ip, "/mode")
    if isinstance(mode, dict):
        state["mode"] = mode.get("mode", "?")

    if hw.startswith("RSWAVE"):
        # Read wave program
        auto = http_get(ip, "/auto")
        if isinstance(auto, dict):
            intervals = auto.get("intervals", [])
            state["intervals_count"] = len(intervals)
            if intervals:
                # Show first interval summary
                first = intervals[0]
                state["wave_type"] = first.get("type", "?")
                state["wave_fti"] = first.get("fti", "?")
                state["wave_direction"] = first.get("direction", "?")
            state["auto_raw"] = auto

    elif hw.startswith("RSRUN"):
        # Read pump settings
        pump_index = ctrl.get("pump_index", "pump_1")
        settings = http_get(ip, "/pump/settings")
        if isinstance(settings, dict):
            pump = settings.get(pump_index, {})
            state["schedule_enabled"] = pump.get("schedule_enabled", "?")
            schedule = pump.get("schedule", [])
            state["schedule_slots"] = len(schedule)
            if schedule:
                # Show first slot
                first = schedule[0]
                state["first_slot_intensity"] = first.get("ti", "?")
                state["first_slot_start"] = first.get("st", "?")
            state["settings_raw"] = settings
            state["pump_index"] = pump_index

    return state


def print_device_state(state: Dict, label: str = ""):
    """Pretty-print a device state."""
    prefix = f"[{label}] " if label else ""
    if not state.get("reachable", True):
        fail(f"{prefix}{state['key']} @ {state['ip']} — UNREACHABLE")
        return

    hw = state.get("hw_model", "?")
    name = state.get("name", "?")
    mode = state.get("mode", "?")
    print(f"  {C.BOLD}{prefix}{state['key']}{C.END}")
    print(f"    IP: {state['ip']}  |  Model: {hw}  |  Name: {name}")
    print(f"    Mode: {mode}  |  FW: {state.get('fw_version', '?')}")

    if hw.startswith("RSWAVE"):
        wtype = state.get("wave_type", "?")
        fti = state.get("wave_fti", "?")
        direction = state.get("wave_direction", "?")
        n = state.get("intervals_count", 0)
        wave_labels = {"un": "Uniform", "co": "Constant", "pw": "Pulse",
                       "rl": "Roll", "sw": "Sway", "ab": "AB Wave",
                       "el": "Else", "rn": "Random"}
        wtype_label = wave_labels.get(wtype, wtype)
        print(f"    Wave: {wtype_label} @ {fti}% ({direction})")
        print(f"    Intervals: {n}")

    elif hw.startswith("RSRUN"):
        enabled = state.get("schedule_enabled", "?")
        slots = state.get("schedule_slots", 0)
        first_ti = state.get("first_slot_intensity", "?")
        pump_idx = state.get("pump_index", "?")
        pump_name = state.get("pump_name", pump_idx)
        print(f"    Pump: {pump_idx}  |  Schedule: {'ON' if enabled else 'OFF'}")
        print(f"    Slots: {slots}  |  First slot intensity: {first_ti}%")


def states_match(before: Dict, after: Dict) -> bool:
    """Compare two device states to check if restore was successful."""
    hw = before.get("hw_model", "")

    if hw.startswith("RSWAVE"):
        # Compare wave type, intensity, interval count
        return (before.get("wave_type") == after.get("wave_type")
                and before.get("wave_fti") == after.get("wave_fti")
                and before.get("intervals_count") == after.get("intervals_count"))

    elif hw.startswith("RSRUN"):
        # Compare schedule enabled, slot count, first slot intensity
        return (before.get("schedule_enabled") == after.get("schedule_enabled")
                and before.get("schedule_slots") == after.get("schedule_slots")
                and before.get("first_slot_intensity") == after.get("first_slot_intensity"))

    return True  # Unknown: assume OK


# =============================================================================
# Test cycle
# =============================================================================

TEST_INTENSITY = 50
TEST_DURATION_S = 5


def test_pump(ctrl: dict, dry_run: bool = False) -> bool:
    """
    Run the full test cycle on a single pump:
      1. Read current state
      2. Apply test intensity (50%)
      3. Verify change applied
      4. Wait 5 seconds
      5. Restore original
      6. Verify restoration
    Returns True if all steps passed.
    """
    key = ctrl["key"]
    ip = ctrl["ip"]
    hw = ctrl["hw_model"]
    label = ctrl.get("pump_name") or ctrl.get("name", key)

    section(f"Testing: {label} ({key})")
    all_ok = True

    # Step 1: Read current state
    info("Step 1/6: Reading current state...")
    before = read_device_state(ctrl)
    if not before or not before.get("reachable"):
        fail(f"Cannot reach {key} @ {ip}")
        return False
    print_device_state(before, "BEFORE")
    ok("Current state read successfully")

    if dry_run:
        info("[DRY RUN] Would apply test intensity, verify, then restore")
        return True

    # Step 2: Apply test intensity
    info(f"Step 2/6: Applying test intensity ({TEST_INTENSITY}%)...")
    if hw.startswith("RSWAVE"):
        warn("ReefWave uses local API — the Red Sea mobile app will NOT "
             "reflect this change (cloud desync). This is normal.")
    apply_ok = False
    if hw.startswith("RSWAVE"):
        apply_ok = _rswave_apply_test(ctrl, TEST_INTENSITY)
    elif hw.startswith("RSRUN"):
        apply_ok = _rsrun_apply_test(ctrl, TEST_INTENSITY)

    if apply_ok:
        ok(f"Test intensity ({TEST_INTENSITY}%) applied")
    else:
        fail("Failed to apply test intensity")
        all_ok = False

    # Step 3: Verify change
    info("Step 3/6: Waiting 30s for device to apply changes...")
    time.sleep(30)
    info("Step 3/6: Verifying change was applied...")
    during = read_device_state(ctrl)
    if during:
        print_device_state(during, "DURING")
        # Check that the state actually changed
        if not states_match(before, during):
            ok("State changed — test intensity applied correctly")
        else:
            warn("State appears unchanged — device may have rejected the command")
            all_ok = False
    else:
        warn("Cannot read state during test")

    # Step 4: Wait
    info(f"Step 4/6: Waiting {TEST_DURATION_S}s...")
    time.sleep(TEST_DURATION_S)

    # Step 5: Restore original
    info("Step 5/6: Restoring original configuration...")
    restore_ok = False
    if hw.startswith("RSWAVE"):
        restore_ok = _rswave_restore_test(ctrl, before)
    elif hw.startswith("RSRUN"):
        restore_ok = _rsrun_restore_test(ctrl, before)

    if restore_ok:
        ok("Original configuration restored")
        if hw.startswith("RSWAVE"):
            info("The ReefWave is back to its original schedule locally.")
            info("The Red Sea cloud/app will re-sync on next app connection.")
    else:
        fail("RESTORE FAILED — check device manually!")
        if hw.startswith("RSWAVE"):
            warn("The ReefWave may still be running the test schedule!")
            warn("Open the ReefBeat app or push the original config manually.")
        all_ok = False

    # Step 6: Verify restoration
    info("Step 6/6: Waiting 10s for device to restore...")
    time.sleep(10)
    info("Step 6/6: Verifying restoration...")
    after = read_device_state(ctrl)
    if after:
        print_device_state(after, "AFTER")
        if states_match(before, after):
            ok("Restoration verified — state matches original")
        else:
            warn("State differs from original after restore")
            warn("  This may be normal if the device recomputed intervals")
            all_ok = False
    else:
        warn("Cannot read state after restore")

    # Summary
    print()
    if all_ok:
        ok(f"{C.BOLD}TEST PASSED{C.END} — {label}")
    else:
        warn(f"{C.BOLD}TEST COMPLETED WITH WARNINGS{C.END} — {label}")

    return all_ok


# =============================================================================
# Per-type apply/restore helpers
# =============================================================================

def _rswave_apply_test(ctrl: dict, intensity: int) -> bool:
    """Apply a uniform wave at the given intensity."""
    import uuid
    ip = ctrl["ip"]
    op_uid = str(uuid.uuid4())
    wave_uid = str(uuid.uuid4())

    # The ReefWave firmware requires each interval to have a "name" field
    # and several other properties, even for uniform wave mode.
    interval = {
        "wave_uid": wave_uid,
        "name": "Backup Test",
        "type": "un",          # uniform: steady continuous flow
        "direction": "fw",
        "frt": 2,              # min 2, max 60
        "rrt": 2,              # min 2, max 60
        "fti": intensity,      # forward target intensity (min 10, max 100)
        "rti": intensity,      # reverse target intensity (min 10, max 100)
        "pd": 2,               # pulse duration (min 2, max 25)
        "sn": 3,               # sine (min 3, max 10)
        "sync": True,
        "st": 0,               # starts at 00:00
        "start": 0,
    }
    body = {"intervals": [interval]}

    return (http_send(ip, "/auto/init", {"uid": op_uid}, "post")
            and http_send(ip, "/auto", body, "post")
            and http_send(ip, "/auto/complete", {"uid": op_uid}, "post")
            and http_send(ip, "/auto/apply", {"uid": op_uid}, "post"))


def _rswave_restore_test(ctrl: dict, before_state: Dict) -> bool:
    """Restore a ReefWave from the state captured before the test."""
    import uuid
    ip = ctrl["ip"]
    auto = before_state.get("auto_raw")
    if not isinstance(auto, dict):
        fail("No auto_raw in before state — cannot restore")
        return False

    op_uid = str(uuid.uuid4())
    body = dict(auto)
    body.pop("uid", None)

    return (http_send(ip, "/auto/init", {"uid": op_uid}, "post")
            and http_send(ip, "/auto", body, "post")
            and http_send(ip, "/auto/complete", {"uid": op_uid}, "post")
            and http_send(ip, "/auto/apply", {"uid": op_uid}, "post"))


def _rsrun_apply_test(ctrl: dict, intensity: int) -> bool:
    """Apply a 1-slot schedule at the given intensity."""
    ip = ctrl["ip"]
    pump_index = ctrl.get("pump_index", "pump_1")
    payload = {
        pump_index: {
            "schedule_enabled": True,
            "schedule": [{"st": 0, "pd": 0, "ti": intensity}],
        }
    }
    return http_send(ip, "/pump/settings", payload, "put")


def _rsrun_restore_test(ctrl: dict, before_state: Dict) -> bool:
    """Restore a ReefRun pump from the state captured before the test."""
    ip = ctrl["ip"]
    pump_index = ctrl.get("pump_index", "pump_1")
    settings = before_state.get("settings_raw")
    if not isinstance(settings, dict):
        fail("No settings_raw in before state — cannot restore")
        return False

    pump_data = settings.get(pump_index, {})
    payload = {
        pump_index: {
            "schedule_enabled": bool(pump_data.get("schedule_enabled", True)),
            "schedule": pump_data.get("schedule", []),
        }
    }
    return http_send(ip, "/pump/settings", payload, "put")


# =============================================================================
# Main
# =============================================================================

def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        fail(f"Config not found: {path}")
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def main():
    global DEBUG

    parser = argparse.ArgumentParser(
        description="reefbeat Backup -- ReefBeat equipment test"
    )
    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to config.json")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List configured devices")
    parser.add_argument("--read", "-r", action="store_true",
                        help="Read current state of all devices")
    parser.add_argument("--test-all", "-a", action="store_true",
                        help="Run full test on all devices")
    parser.add_argument("--test", "-t", default=None,
                        help="Test a specific pump by key")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would be done without changes")
    parser.add_argument("--debug", "-D", action="store_true",
                        help="Show HTTP requests and responses")
    args = parser.parse_args()

    global DEBUG
    DEBUG = args.debug

    cfg = load_config(args.config)
    controllers = cfg.get("pump_control", {}).get("controllers", [])

    if not controllers:
        fail("No controllers configured in config.json")
        sys.exit(1)

    header("reefbeat Backup — Equipment Test")

    # --list
    if args.list:
        section("Configured devices")
        for i, ctrl in enumerate(controllers, 1):
            key = ctrl["key"]
            ip = ctrl["ip"]
            hw = ctrl["hw_model"]
            pump_name = ctrl.get("pump_name") or ctrl.get("name", "")
            print(f"  {C.BOLD}{i}.{C.END} {key}")
            print(f"     IP: {ip}  |  Model: {hw}  |  Name: {pump_name}")
        return

    # --read
    if args.read:
        section("Current device states")
        for ctrl in controllers:
            state = read_device_state(ctrl)
            if state:
                print_device_state(state)
            print()
        return

    # --test or --test-all
    if args.test:
        # Find the controller by key
        target = None
        for ctrl in controllers:
            if ctrl["key"] == args.test:
                target = ctrl
                break
        if not target:
            fail(f"Pump key '{args.test}' not found")
            info("Available keys:")
            for ctrl in controllers:
                info(f"  {ctrl['key']}")
            sys.exit(1)
        test_pump(target, dry_run=args.dry_run)
        return

    if args.test_all:
        section("Running full test on all devices")
        results = []
        for ctrl in controllers:
            passed = test_pump(ctrl, dry_run=args.dry_run)
            results.append((ctrl["key"], passed))

        # Summary
        header("Test Summary")
        passed = sum(1 for _, p in results if p)
        total = len(results)
        for key, p in results:
            icon = f"{C.OK}✓{C.END}" if p else f"{C.WARN}⚠{C.END}"
            print(f"  {icon} {key}")
        print()
        if passed == total:
            ok(f"{C.BOLD}All {total} tests passed!{C.END}")
        else:
            warn(f"{C.BOLD}{passed}/{total} tests passed{C.END}")
        return

    # Interactive mode (no arguments)
    def show_menu():
        section("Configured devices")
        for i, ctrl in enumerate(controllers, 1):
            key = ctrl["key"]
            ip = ctrl["ip"]
            pump_name = ctrl.get("pump_name") or ctrl.get("name", "")
            print(f"  {C.BOLD}{i}.{C.END} {pump_name} ({key}) @ {ip}")
        print()
        print(f"  {C.BOLD}Options:{C.END}")
        print(f"    {C.BOLD}r{C.END} — Read current state of all devices")
        print(f"    {C.BOLD}a{C.END} — Test ALL devices (full cycle)")
        print(f"    {C.BOLD}1-{len(controllers)}{C.END} — Test a specific device")
        print(f"    {C.BOLD}d{C.END} — Dry run (show actions without applying)")
        dbg_label = "ON" if DEBUG else "OFF"
        print(f"    {C.BOLD}v{C.END} — Toggle debug mode (currently {dbg_label})")
        print(f"    {C.BOLD}q{C.END} — Quit")
        print()

    show_menu()

    while True:
        try:
            choice = input(f"  {C.BOLD}?{C.END} Your choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice == "q":
            return
        elif choice == "v":
            DEBUG = not DEBUG
            ok(f"Debug mode {'ON' if DEBUG else 'OFF'}")
            show_menu()
        elif choice == "r":
            for ctrl in controllers:
                state = read_device_state(ctrl)
                if state:
                    print_device_state(state)
                print()
            show_menu()
        elif choice == "a":
            for ctrl in controllers:
                test_pump(ctrl)
            show_menu()
        elif choice == "d":
            for ctrl in controllers:
                test_pump(ctrl, dry_run=True)
            show_menu()
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(controllers):
                test_pump(controllers[idx])
            else:
                warn(f"Invalid number. Choose 1-{len(controllers)}")
            show_menu()
        else:
            warn("Invalid choice")
            show_menu()


if __name__ == "__main__":
    main()
