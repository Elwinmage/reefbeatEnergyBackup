"""
reefbeat⚡Backup — Self-update module.

Periodically checks GitHub for new releases, publishes an HA update
entity via MQTT, and performs self-update when triggered via MQTT
command or CLI.

HA integration:
  - Publishes an `update` entity showing current vs latest version
  - Listens on MQTT for install command from HA "Install" button
  - After update: restarts the systemd service automatically

Configuration (config.json):
  "updater": {
      "enabled": true,
      "check_interval_h": 6,
      "repo": "Elwinmage/reefbeatEnergyBackup",
      "branch": "main",
      "auto_restart": true
  }
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Tuple

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# =============================================================================
# Version management
# =============================================================================

VERSION_FILE = "VERSION"
DEFAULT_VERSION = "0.0.0"


def get_current_version(install_dir: str) -> str:
    """Read current version from VERSION file."""
    vf = Path(install_dir) / VERSION_FILE
    if vf.exists():
        return vf.read_text().strip()
    # Fallback: try git describe
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5,
            cwd=install_dir,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return DEFAULT_VERSION


def get_latest_release(repo: str) -> Optional[Dict]:
    """Fetch latest release info from GitHub API."""
    if not HAS_REQUESTS:
        return None
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        r = requests.get(url, timeout=10, headers={
            "Accept": "application/vnd.github.v3+json",
        })
        if r.status_code == 200:
            data = r.json()
            return {
                "version": data.get("tag_name", "").lstrip("v"),
                "name": data.get("name", ""),
                "url": data.get("html_url", ""),
                "tarball": data.get("tarball_url", ""),
                "body": data.get("body", "")[:500],  # Truncate changelog
                "published": data.get("published_at", ""),
            }
        elif r.status_code == 404:
            # No releases yet — check latest commit on main
            return _get_latest_commit(repo)
    except Exception as e:
        print(f"[UPDATER] GitHub API error: {e}")
    return None


def _get_latest_commit(repo: str) -> Optional[Dict]:
    """Fallback: get latest commit SHA when no releases exist."""
    try:
        url = f"https://api.github.com/repos/{repo}/commits/main"
        r = requests.get(url, timeout=10, headers={
            "Accept": "application/vnd.github.v3+json",
        })
        if r.status_code == 200:
            data = r.json()
            sha = data.get("sha", "")[:7]
            msg = data.get("commit", {}).get("message", "").split("\n")[0]
            return {
                "version": sha,
                "name": msg,
                "url": data.get("html_url", ""),
                "tarball": "",
                "body": msg,
                "published": data.get("commit", {}).get("author", {}).get("date", ""),
            }
    except Exception:
        pass
    return None


def version_is_newer(current: str, latest: str) -> bool:
    """Compare version strings. Handles semver and commit SHAs."""
    if not current or not latest:
        return False
    if current == latest:
        return False

    # Try semver comparison
    try:
        def parse_ver(v):
            v = v.lstrip("v")
            parts = v.split(".")
            return tuple(int(p) for p in parts)
        return parse_ver(latest) > parse_ver(current)
    except (ValueError, IndexError):
        pass

    # Fallback: string comparison (for commit SHAs, always "different")
    return current != latest


# =============================================================================
# Update execution
# =============================================================================

def perform_update(install_dir: str, repo: str, branch: str = "main",
                   release_info: Dict = None) -> Tuple[bool, str]:
    """
    Download and install the latest version.

    Strategy:
      1. Try tarball from release (if available)
      2. Fallback to git pull

    Returns (success: bool, message: str).
    """
    install_path = Path(install_dir)

    # Backup current config
    config_path = install_path / "config.json"
    if config_path.exists():
        from datetime import datetime
        backup = f"config.json.save.{datetime.now().strftime('%Y%m%d.%H.%M.%S')}"
        (install_path / backup).write_text(config_path.read_text())
        print(f"[UPDATER] Config backed up: {backup}")

    # Method 1: git pull (if .git exists)
    git_dir = install_path / ".git"
    if git_dir.exists():
        try:
            # Stash any local changes
            subprocess.run(
                ["git", "stash"], capture_output=True,
                cwd=install_dir, timeout=10)

            # Pull latest
            result = subprocess.run(
                ["git", "pull", "origin", branch],
                capture_output=True, text=True,
                cwd=install_dir, timeout=30)

            if result.returncode == 0:
                # Restore config
                subprocess.run(
                    ["git", "stash", "pop"], capture_output=True,
                    cwd=install_dir, timeout=10)

                new_ver = get_current_version(install_dir)
                return True, f"Updated to {new_ver} via git pull"
            else:
                return False, f"git pull failed: {result.stderr[:200]}"
        except Exception as e:
            return False, f"git error: {e}"

    # Method 2: download tarball
    if release_info and release_info.get("tarball"):
        try:
            tarball_url = release_info["tarball"]
            result = subprocess.run([
                "curl", "-sL", tarball_url,
                "-o", "/tmp/reef_update.tar.gz"
            ], capture_output=True, timeout=60)

            if result.returncode != 0:
                return False, "Failed to download tarball"

            # Extract (skip first component = repo-name-sha/)
            result = subprocess.run([
                "tar", "xzf", "/tmp/reef_update.tar.gz",
                "--strip-components=1", "-C", install_dir
            ], capture_output=True, timeout=30)

            os.remove("/tmp/reef_update.tar.gz")

            if result.returncode == 0:
                new_ver = get_current_version(install_dir)
                return True, f"Updated to {new_ver} via tarball"
            else:
                return False, "Failed to extract tarball"
        except Exception as e:
            return False, f"tarball error: {e}"

    # Method 3: clone fresh
    try:
        tmp_dir = "/tmp/reef_update_clone"
        subprocess.run(["rm", "-rf", tmp_dir], capture_output=True)
        result = subprocess.run([
            "git", "clone", "--depth", "1",
            f"https://github.com/{repo}.git", tmp_dir
        ], capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            # Copy all files except .git and config.json
            for item in Path(tmp_dir).iterdir():
                if item.name in (".git", "config.json"):
                    continue
                dest = install_path / item.name
                if item.is_dir():
                    import shutil
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    dest.write_bytes(item.read_bytes())

            subprocess.run(["rm", "-rf", tmp_dir], capture_output=True)
            new_ver = get_current_version(install_dir)
            return True, f"Updated to {new_ver} via fresh clone"
        else:
            return False, f"clone failed: {result.stderr[:200]}"
    except Exception as e:
        return False, f"clone error: {e}"


def restart_service(service_name: str = "reefbeat-energy-backup") -> bool:
    """Restart the systemd service after update."""
    try:
        # Install any new Python dependencies
        install_dir = os.path.dirname(os.path.abspath(__file__))
        req_file = os.path.join(install_dir, "requirements.txt")
        if os.path.exists(req_file):
            subprocess.run([
                "pip3", "install", "--break-system-packages",
                "-r", req_file
            ], capture_output=True, timeout=60)

        result = subprocess.run(
            ["sudo", "systemctl", "restart", service_name],
            capture_output=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False


# =============================================================================
# Updater service (runs in background thread)
# =============================================================================

class Updater:
    """
    Background updater that:
      1. Checks GitHub for new versions periodically
      2. Publishes HA update entity via MQTT
      3. Listens for install command via MQTT
      4. Performs update and restarts service
    """

    def __init__(self, cfg: dict, mqtt_client, install_dir: str):
        self._cfg = cfg.get("updater", {})
        self._mqtt_cfg = cfg.get("mqtt", {})
        self._client = mqtt_client
        self._install_dir = install_dir
        self._enabled = self._cfg.get("enabled", True)
        self._repo = self._cfg.get("repo", "Elwinmage/reefbeatEnergyBackup")
        self._branch = self._cfg.get("branch", "main")
        self._check_interval = self._cfg.get("check_interval_h", 6) * 3600
        self._auto_restart = self._cfg.get("auto_restart", True)

        self._current_version = get_current_version(install_dir)
        self._latest_info: Optional[Dict] = None
        self._update_available = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        if self._enabled:
            print(f"[UPDATER] Current version: {self._current_version}")
            print(f"[UPDATER] Repo: {self._repo}, check every "
                  f"{self._cfg.get('check_interval_h', 6)}h")

    def start(self):
        """Start the background check thread and subscribe to MQTT commands."""
        if not self._enabled:
            return

        # Subscribe to update command topic
        if self._client:
            device = self._mqtt_cfg.get("device_name", "reef_battery")
            base = self._mqtt_cfg.get("base_topic", "homeassistant")
            cmd_topic = f"{base}/update/{device}_update/command"
            self._client.subscribe(cmd_topic)
            self._client.message_callback_add(cmd_topic, self._on_mqtt_command)
            print(f"[UPDATER] Listening for commands on {cmd_topic}")

        # Publish HA discovery for update entity
        self._publish_ha_discovery()

        # Start background check thread
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _check_loop(self):
        """Periodically check for updates."""
        # First check after 30s (let everything initialize)
        if self._stop.wait(timeout=30):
            return

        while not self._stop.is_set():
            self._check_for_update()
            if self._stop.wait(timeout=self._check_interval):
                return

    def _check_for_update(self):
        """Check GitHub for a new version and publish state."""
        print("[UPDATER] Checking for updates...")
        info = get_latest_release(self._repo)

        if info:
            self._latest_info = info
            self._update_available = version_is_newer(
                self._current_version, info["version"]
            )

            if self._update_available:
                print(f"[UPDATER] New version available: "
                      f"{self._current_version} → {info['version']}")
            else:
                print(f"[UPDATER] Up to date ({self._current_version})")
        else:
            print("[UPDATER] Cannot reach GitHub")

        self._publish_state()

    def _on_mqtt_command(self, client, userdata, msg):
        """Handle MQTT install command from HA."""
        try:
            payload = msg.payload.decode("utf-8").strip()
            if payload == "install":
                print("[UPDATER] Install command received from HA")
                self._do_update()
        except Exception as e:
            print(f"[UPDATER] Command error: {e}")

    def _do_update(self):
        """Perform the update."""
        self._publish_state(in_progress=True)

        success, message = perform_update(
            self._install_dir, self._repo, self._branch,
            self._latest_info
        )

        if success:
            print(f"[UPDATER] {message}")
            self._current_version = get_current_version(self._install_dir)
            self._update_available = False
            self._publish_state()

            if self._auto_restart:
                print("[UPDATER] Restarting service in 3s...")
                time.sleep(3)
                restart_service()
        else:
            print(f"[UPDATER] Update failed: {message}")
            self._publish_state()

    # =========================================================================
    # MQTT / HA integration
    # =========================================================================

    def _publish_ha_discovery(self):
        """Publish MQTT discovery for HA update entity."""
        if not self._client:
            return

        device = self._mqtt_cfg.get("device_name", "reef_battery")
        base = self._mqtt_cfg.get("base_topic", "homeassistant")

        device_info = {
            "identifiers": [device],
            "name": "Reef Battery Backup",
            "manufacturer": "reefbeat⚡Backup",
            "model": "Energy Backup System",
        }

        # HA update entity discovery
        discovery = {
            "name": "Mise a jour logicielle",
            "unique_id": f"{device}_update",
            "device": device_info,
            "state_topic": f"{base}/update/{device}_update/state",
            "command_topic": f"{base}/update/{device}_update/command",
            "payload_install": "install",
            "entity_picture": "https://raw.githubusercontent.com/"
                              f"{self._repo}/main/docs/images/icon.png",
            "release_url": f"https://github.com/{self._repo}/releases",
        }

        topic = f"{base}/update/{device}_update/config"
        self._client.publish(topic, json.dumps(discovery), retain=True)
        print("[UPDATER] Published HA update entity discovery")

    def _publish_state(self, in_progress: bool = False):
        """Publish current update state to HA."""
        if not self._client:
            return

        device = self._mqtt_cfg.get("device_name", "reef_battery")
        base = self._mqtt_cfg.get("base_topic", "homeassistant")

        latest_ver = (self._latest_info.get("version", "")
                      if self._latest_info else self._current_version)
        changelog = (self._latest_info.get("body", "")
                     if self._latest_info else "")
        release_url = (self._latest_info.get("url", "")
                       if self._latest_info else "")

        state = {
            "installed_version": self._current_version,
            "latest_version": latest_ver,
            "title": "reefbeat Backup",
            "release_summary": changelog[:200] if changelog else None,
            "release_url": release_url,
            "in_progress": in_progress,
        }

        topic = f"{base}/update/{device}_update/state"
        self._client.publish(topic, json.dumps(state), retain=True)

    # =========================================================================
    # CLI
    # =========================================================================

    def check_now(self) -> bool:
        """Manual check (for CLI use)."""
        self._check_for_update()
        return self._update_available

    def update_now(self) -> Tuple[bool, str]:
        """Manual update (for CLI use)."""
        if not self._latest_info:
            self._check_for_update()
        return perform_update(
            self._install_dir, self._repo, self._branch,
            self._latest_info
        )


# =============================================================================
# Factory
# =============================================================================

def create_updater(cfg: dict, mqtt_client,
                   install_dir: str) -> Updater:
    """Create the updater from config."""
    return Updater(cfg, mqtt_client, install_dir)
