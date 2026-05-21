#!/usr/bin/env python3
"""
reefbeat⚡Backup — Notification test CLI

Send test notifications from the command line to verify your setup.

Usage:
  python3 test_notif.py                    # Send a simple test
  python3 test_notif.py --type outage      # Simulate outage alert
  python3 test_notif.py --type restored    # Simulate power restored
  python3 test_notif.py --type critical    # Simulate critical battery
  python3 test_notif.py --type level       # Simulate level change
  python3 test_notif.py --type network     # Simulate network failover
  python3 test_notif.py --message "Hello"  # Custom message
  python3 test_notif.py --lte              # Force send via LTE modem
  python3 test_notif.py --config /path/to/config.json
"""

import argparse
import json
import sys
from pathlib import Path


def load_config(path: str) -> dict:
    """Load config.json."""
    p = Path(path)
    if not p.exists():
        print(f"Error: {path} not found")
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="reefbeat Backup -- Test notifications"
    )
    parser.add_argument(
        "--config", "-c", default="config.json",
        help="Path to config.json (default: ./config.json)"
    )
    parser.add_argument(
        "--type", "-t", default="test",
        choices=["test", "outage", "restored", "critical", "level", "network"],
        help="Type of notification to send"
    )
    parser.add_argument(
        "--message", "-m", default=None,
        help="Custom message to send"
    )
    parser.add_argument(
        "--lte", action="store_true",
        help="Force send via LTE modem (skip Wi-Fi)"
    )
    parser.add_argument(
        "--priority", "-p", default=None,
        choices=["default", "low", "high", "urgent"],
        help="Override notification priority"
    )
    args = parser.parse_args()

    # Load config
    cfg = load_config(args.config)
    notif_cfg = cfg.get("notifications", {})

    if not notif_cfg.get("enabled"):
        print("Notifications are disabled in config.json")
        print("Set notifications.enabled = true and configure ntfy topic.")
        sys.exit(1)

    ntfy_cfg = notif_cfg.get("ntfy", {})
    server = ntfy_cfg.get("server", "https://ntfy.sh")
    topic = ntfy_cfg.get("topic", "")

    if not topic:
        print("Error: ntfy topic not configured in config.json")
        sys.exit(1)

    print(f"Server : {server}")
    print(f"Topic  : {topic}")
    print(f"Type   : {args.type}")
    print()

    # Custom message mode
    if args.message:
        _send_raw(server, topic, args.message,
                  priority=args.priority or "default",
                  lte=args.lte, cfg=notif_cfg)
        return

    # Simulated notification types
    if args.type == "test":
        _send_raw(server, topic,
                  "reefbeat Backup -- Test notification OK!\n"
                  "Si vous voyez ce message, les notifications fonctionnent.",
                  title="reefbeat Backup -- Test",
                  tags="tropical_fish,zap",
                  priority=args.priority or "default",
                  lte=args.lte, cfg=notif_cfg)

    elif args.type == "outage":
        _send_raw(server, topic,
                  "La batterie de secours a pris le relais.\n"
                  "SoC : 95%\n"
                  "Autonomie estimee : 14h",
                  title="Coupure de courant detectee",
                  tags="zap,warning",
                  priority=args.priority or ntfy_cfg.get("priority_outage", "high"),
                  lte=args.lte, cfg=notif_cfg)

    elif args.type == "restored":
        _send_raw(server, topic,
                  "Le courant est revenu apres 2h15.\n"
                  "SoC batterie : 72%\n"
                  "La batterie va se recharger.",
                  title="Courant retabli",
                  tags="white_check_mark,zap",
                  priority=args.priority or ntfy_cfg.get("priority_info", "default"),
                  lte=args.lte, cfg=notif_cfg)

    elif args.type == "critical":
        _send_raw(server, topic,
                  "SoC a 8% !\n"
                  "Autonomie restante : < 1h\n"
                  "Intervention urgente requise.",
                  title="BATTERIE CRITIQUE",
                  tags="rotating_light,sos",
                  priority=args.priority or ntfy_cfg.get("priority_critical", "urgent"),
                  lte=args.lte, cfg=notif_cfg)

    elif args.type == "level":
        _send_raw(server, topic,
                  "SoC batterie : 55%\n"
                  "Autonomie estimee : 8h\n"
                  "Les pompes ont ete ajustees.",
                  title="Niveau survival active",
                  tags="orange_circle,warning",
                  priority=args.priority or ntfy_cfg.get("priority_outage", "high"),
                  lte=args.lte, cfg=notif_cfg)

    elif args.type == "network":
        _send_raw(server, topic,
                  "Mode reseau : hotspot\n"
                  "Le RPi a cree un point d'acces Wi-Fi miroir.",
                  title="Hotspot Wi-Fi active",
                  tags="satellite,wifi",
                  priority=args.priority or ntfy_cfg.get("priority_info", "default"),
                  lte=args.lte, cfg=notif_cfg)


def _send_raw(server: str, topic: str, message: str,
              title: str = "reefbeat Backup",
              tags: str = "zap",
              priority: str = "default",
              lte: bool = False,
              cfg: dict = None):
    """Send a notification, optionally forcing LTE."""

    import subprocess

    url = f"{server}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }

    # If --lte, try LTE first
    if lte:
        print("Forcing LTE modem...")
        iface = _detect_lte_interface()
        if iface:
            print(f"Using interface: {iface}")
            try:
                cmd = [
                    "curl", "-s",
                    "--interface", iface,
                    "-H", f"Title: {title}",
                    "-H", f"Priority: {priority}",
                    "-H", f"Tags: {tags}",
                    "-d", message,
                    url
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=15)
                if result.returncode == 0:
                    print("Sent via LTE!")
                    return
                else:
                    print(f"LTE send failed: {result.stderr.decode()[:100]}")
            except Exception as e:
                print(f"LTE error: {e}")
        else:
            print("No LTE modem detected, falling back to Wi-Fi")

    # Normal send via Wi-Fi
    try:
        import requests
        r = requests.post(url, data=message.encode("utf-8"),
                          headers=headers, timeout=10)
        if r.status_code == 200:
            print("Sent OK!")
        else:
            print(f"Failed: HTTP {r.status_code}")
            print(r.text[:200])
    except Exception as e:
        print(f"Error: {e}")


def _detect_lte_interface() -> str:
    """Detect LTE modem network interface."""
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "route"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if "192.168.8.1" in line:
                import re
                match = re.search(r'dev\s+(\S+)', line)
                if match:
                    return match.group(1)
    except Exception:
        pass

    # Fallback: check common interface names
    for iface in ["eth1", "usb0", "wwan0"]:
        try:
            result = subprocess.run(
                ["ip", "link", "show", iface],
                capture_output=True, timeout=3
            )
            if result.returncode == 0:
                return iface
        except Exception:
            pass

    return None


if __name__ == "__main__":
    main()
