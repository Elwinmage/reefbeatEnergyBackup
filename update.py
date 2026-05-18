#!/usr/bin/env python3
"""
reefbeat⚡Backup — Update CLI

Check for updates and install them from the command line.

Usage:
  python3 update.py              # Check for updates
  python3 update.py --install    # Check and install if available
  python3 update.py --force      # Force reinstall latest version
  python3 update.py --version    # Show current version
"""

import argparse
import json
import sys
from pathlib import Path

from updater import (
    get_current_version,
    get_latest_release,
    version_is_newer,
    perform_update,
    restart_service,
)


def main():
    parser = argparse.ArgumentParser(
        description="reefbeat Backup -- Update manager"
    )
    parser.add_argument(
        "--install", "-i", action="store_true",
        help="Install update if available"
    )
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Force reinstall latest version"
    )
    parser.add_argument(
        "--version", "-v", action="store_true",
        help="Show current version and exit"
    )
    parser.add_argument(
        "--no-restart", action="store_true",
        help="Don't restart service after update"
    )
    parser.add_argument(
        "--config", "-c", default="config.json",
        help="Path to config.json"
    )
    args = parser.parse_args()

    install_dir = str(Path(__file__).parent)
    current = get_current_version(install_dir)

    if args.version:
        print(current)
        return

    # Load config for repo info
    config_path = Path(args.config)
    repo = "Elwinmage/reefbeatEnergyBackup"
    branch = "main"
    if config_path.exists():
        try:
            cfg = json.load(open(config_path))
            updater_cfg = cfg.get("updater", {})
            repo = updater_cfg.get("repo", repo)
            branch = updater_cfg.get("branch", branch)
        except Exception:
            pass

    print(f"  Current version : {current}")
    print(f"  Repository      : {repo}")
    print()

    # Check for update
    print("Checking for updates...")
    info = get_latest_release(repo)

    if not info:
        print("Cannot reach GitHub. Check your internet connection.")
        sys.exit(1)

    latest = info["version"]
    print(f"  Latest version  : {latest}")

    if info.get("name"):
        print(f"  Release name    : {info['name']}")
    if info.get("published"):
        print(f"  Published       : {info['published']}")
    if info.get("body"):
        print(f"  Changelog       : {info['body'][:200]}")
    print()

    is_newer = version_is_newer(current, latest)

    if is_newer:
        print(f"  ** Update available: {current} -> {latest} **")
    elif args.force:
        print(f"  Forcing reinstall of {latest}")
    else:
        print("  Already up to date.")
        return

    if not args.install and not args.force:
        print()
        print("  Run with --install to update:")
        print(f"    python3 {sys.argv[0]} --install")
        return

    # Perform update
    print()
    print("Installing update...")
    success, message = perform_update(install_dir, repo, branch, info)

    if success:
        print(f"  OK: {message}")
        new_ver = get_current_version(install_dir)
        print(f"  New version: {new_ver}")

        if not args.no_restart:
            print()
            print("Restarting service...")
            if restart_service():
                print("  Service restarted successfully.")
            else:
                print("  Service restart failed. Run manually:")
                print("    sudo systemctl restart reefbeat-energy-backup")
    else:
        print(f"  FAILED: {message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
