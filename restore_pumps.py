#!/usr/bin/env python3
"""
Manually restore Red Sea pumps to their pre-outage configuration.

Background
----------
During a power outage the service reduces pump intensity and saves each
pump's original configuration to a snapshot on disk
(/var/lib/reefbeat-energy-backup/snapshots/ by default). When mains
returns the service restores the originals automatically, retrying in the
background while the Red Sea devices rejoin Wi-Fi.

This standalone tool re-runs that restore on demand, using the same
snapshots. Use it if the automatic retries gave up (devices took too long
to come back), or any time you want to force a restore from the CLI.

Usage
-----
  python3 restore_pumps.py                 # use ./config.json
  python3 restore_pumps.py /path/config.json
  python3 restore_pumps.py --list          # show pending snapshots, do nothing
  python3 restore_pumps.py --retries 20    # attempts (default 10)
  python3 restore_pumps.py --interval 20   # seconds between attempts (default 15)

Exit code is 0 if all pumps were restored, 1 otherwise.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from controller import PumpController


def load_config(path: str) -> dict:
    """Load config.json, mirroring main.py's loader."""
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Config not found: {path}")
        sys.exit(2)
    with open(p) as f:
        return json.load(f)


def list_pending(pump: PumpController) -> list:
    """Return the controllers that still have a snapshot on disk."""
    pending = []
    for ctrl in pump._pump_cfg.get("controllers", []):
        # _load_snapshot is the canonical "is there pending state?" check.
        if pump._load_snapshot(ctrl["key"]) is not None:
            pending.append(ctrl)
    return pending


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manually restore pumps from outage snapshots."
    )
    parser.add_argument("config", nargs="?", default="config.json",
                        help="Path to config.json (default: ./config.json)")
    parser.add_argument("--list", action="store_true",
                        help="List pending snapshots and exit")
    parser.add_argument("--retries", type=int, default=10,
                        help="Max restore attempts (default: 10)")
    parser.add_argument("--interval", type=float, default=15.0,
                        help="Seconds between attempts (default: 15)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # No MQTT client needed for a manual restore; PumpController handles
    # a None client gracefully (it just skips state publication).
    pump = PumpController(mqtt_client=None, cfg=cfg)

    pending = list_pending(pump)
    if not pending:
        print("Nothing to restore: no pump snapshots on disk.")
        print(f"(snapshot dir: {pump._snapshot_dir})")
        return 0

    print(f"{len(pending)} pump(s) with a saved original config:")
    for ctrl in pending:
        print(f"  - {pump._ctrl_label(ctrl)} @ {ctrl.get('ip', '?')}")

    if args.list:
        return 0

    # Retry loop, reusing the controller's single-pass restore.
    for attempt in range(1, args.retries + 1):
        print(f"\n[Attempt {attempt}/{args.retries}]")
        remaining = pump._restore_pass()
        if remaining == 0:
            print("\nAll pumps restored successfully.")
            return 0
        if attempt < args.retries:
            print(f"{remaining} pump(s) still unreachable; "
                  f"retrying in {args.interval:.0f}s...")
            time.sleep(args.interval)

    still = list_pending(pump)
    print(f"\nDone, but {len(still)} pump(s) could not be restored:")
    for ctrl in still:
        print(f"  - {pump._ctrl_label(ctrl)} @ {ctrl.get('ip', '?')}")
    print("Their snapshots are kept on disk; re-run this tool when the "
          "devices are back online.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
