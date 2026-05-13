"""
reefbeat⚡Backup — Push notification module.

Sends push notifications directly to the user's phone via ntfy.sh,
independent of Home Assistant / MQTT. Supports fallback to 4G USB
modem (Huawei E3372 HiLink) when Wi-Fi is unavailable.

Notification events:
  - Power outage detected
  - Power restored (with duration)
  - SoC level change (eco / survival / critical)
  - SoC critical alert (repeated)
  - Network failover status (hotspot activated, etc.)

Configuration (config.json):
  "notifications": {
      "enabled": true,
      "provider": "ntfy",
      "ntfy": {
          "server": "https://ntfy.sh",
          "topic": "reefbeat-CHANGE-ME",
          "priority_outage": "high",
          "priority_critical": "urgent",
          "priority_info": "default"
      },
      "lte_failover": {
          "enabled": true,
          "interface": "auto",
          "check_url": "http://192.168.8.1/api/monitoring/status"
      },
      "cooldown_s": 300
  }
"""

import subprocess
import time
import threading
import re
from enum import Enum
from typing import Optional, Dict
from pathlib import Path

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# =============================================================================
# LTE modem detection and management
# =============================================================================

class LteModem:
    """
    Detects and manages USB 4G/LTE modems (Huawei E3372 HiLink).

    The E3372h creates a virtual Ethernet interface (eth1 or enx...)
    with gateway 192.168.8.1. We detect it by looking for that
    specific gateway in the routing table or by scanning USB devices.
    """

    HILINK_GATEWAY = "192.168.8.1"
    HILINK_STATUS_URL = "http://192.168.8.1/api/monitoring/status"

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._interface: Optional[str] = None
        self._available = False

    def detect(self) -> bool:
        """Detect if a Huawei HiLink modem is connected."""
        # Method 1: Check USB devices
        try:
            result = subprocess.run(
                ["lsusb"], capture_output=True, text=True, timeout=5
            )
            if "Huawei" in result.stdout and ("E3372" in result.stdout
                    or "12d1:" in result.stdout):
                print("[LTE] Huawei USB modem detected via lsusb")
            else:
                print("[LTE] No Huawei USB modem found")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Method 2: Find the network interface
        self._interface = self._find_interface()
        if self._interface:
            print(f"[LTE] Interface found: {self._interface}")
            self._available = True
            return True

        # Method 3: Try to bring up the interface
        try:
            result = subprocess.run(
                ["ip", "link", "show"], capture_output=True, text=True
            )
            # Look for Huawei-style interface names
            for line in result.stdout.split("\n"):
                for prefix in ["eth1", "enx", "usb0", "wwan0"]:
                    if prefix in line:
                        match = re.search(r'\d+:\s+(\S+):', line)
                        if match:
                            iface = match.group(1)
                            # Try DHCP on it
                            subprocess.run(
                                ["sudo", "dhclient", "-nw", iface],
                                capture_output=True, timeout=10
                            )
                            time.sleep(3)
                            if self._check_gateway(iface):
                                self._interface = iface
                                self._available = True
                                print(f"[LTE] Activated interface: {iface}")
                                return True
        except Exception as e:
            print(f"[LTE] Detection error: {e}")

        return False

    def _find_interface(self) -> Optional[str]:
        """Find the network interface connected to the HiLink gateway."""
        try:
            result = subprocess.run(
                ["ip", "route"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if self.HILINK_GATEWAY in line:
                    match = re.search(r'dev\s+(\S+)', line)
                    if match:
                        return match.group(1)
        except Exception:
            pass
        return None

    def _check_gateway(self, interface: str) -> bool:
        """Check if the HiLink gateway is reachable on a given interface."""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", "-I", interface,
                 self.HILINK_GATEWAY],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def interface(self) -> Optional[str]:
        return self._interface

    def is_connected(self) -> bool:
        """Check if the modem has an active cellular connection."""
        if not self._available or not HAS_REQUESTS:
            return False
        try:
            # HiLink API returns connection status
            r = requests.get(self.HILINK_STATUS_URL, timeout=3)
            if r.status_code == 200:
                # ConnectionStatus 901 = connected
                return "901" in r.text
        except Exception:
            pass

        # Fallback: just ping something via the interface
        if self._interface:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "3", "-I", self._interface,
                     "8.8.8.8"],
                    capture_output=True, timeout=5
                )
                return result.returncode == 0
            except Exception:
                pass
        return False


# =============================================================================
# Notification priority
# =============================================================================

class NotifPriority(Enum):
    INFO = "default"
    HIGH = "high"
    URGENT = "urgent"


# =============================================================================
# Notifier
# =============================================================================

class Notifier:
    """
    Sends push notifications via ntfy.sh.

    Features:
      - Sends directly via Wi-Fi when available
      - Falls back to 4G USB modem when Wi-Fi is down
      - Cooldown to avoid notification spam
      - Thread-safe
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg.get("notifications", {})
        self._enabled = self._cfg.get("enabled", False)
        self._provider = self._cfg.get("provider", "ntfy")
        self._cooldowns: Dict[str, float] = {}
        self._cooldown_s = self._cfg.get("cooldown_s", 300)
        self._lock = threading.Lock()

        # ntfy config
        ntfy_cfg = self._cfg.get("ntfy", {})
        self._ntfy_server = ntfy_cfg.get("server", "https://ntfy.sh")
        self._ntfy_topic = ntfy_cfg.get("topic", "")
        self._priority_map = {
            "outage": ntfy_cfg.get("priority_outage", "high"),
            "critical": ntfy_cfg.get("priority_critical", "urgent"),
            "info": ntfy_cfg.get("priority_info", "default"),
        }

        # LTE modem
        lte_cfg = self._cfg.get("lte_failover", {})
        self._lte_enabled = lte_cfg.get("enabled", False)
        self._lte = LteModem(lte_cfg) if self._lte_enabled else None

        if self._enabled:
            if not self._ntfy_topic:
                print("[NOTIF] WARNING: ntfy topic not configured")
                self._enabled = False
            else:
                print(f"[NOTIF] ntfy enabled → {self._ntfy_server}/{self._ntfy_topic}")
                if self._lte_enabled:
                    if self._lte and self._lte.detect():
                        print("[NOTIF] LTE failover available")
                    else:
                        print("[NOTIF] LTE modem not detected (failover disabled)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _can_send(self, event_key: str) -> bool:
        """Check cooldown for an event type."""
        now = time.monotonic()
        with self._lock:
            last = self._cooldowns.get(event_key, 0)
            if now - last < self._cooldown_s:
                return False
            self._cooldowns[event_key] = now
            return True

    def _send_ntfy(self, title: str, message: str,
                   priority: str = "default",
                   tags: str = "") -> bool:
        """Send a notification via ntfy.sh."""
        if not HAS_REQUESTS:
            print("[NOTIF] requests not available")
            return False

        url = f"{self._ntfy_server}/{self._ntfy_topic}"
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": tags,
        }

        # Try normal network first
        try:
            r = requests.post(url, data=message.encode("utf-8"),
                              headers=headers, timeout=10)
            if r.status_code == 200:
                print(f"[NOTIF] Sent via Wi-Fi: {title}")
                return True
            else:
                print(f"[NOTIF] Wi-Fi send failed ({r.status_code})")
        except requests.exceptions.RequestException as e:
            print(f"[NOTIF] Wi-Fi unreachable ({type(e).__name__})")

        # Fallback to LTE
        if self._lte_enabled and self._lte and self._lte.available:
            try:
                iface = self._lte.interface
                if iface and self._lte.is_connected():
                    # Use curl with --interface to force traffic through LTE
                    result = subprocess.run([
                        "curl", "-s", "--interface", iface,
                        "-H", f"Title: {title}",
                        "-H", f"Priority: {priority}",
                        "-H", f"Tags: {tags}",
                        "-d", message,
                        url
                    ], capture_output=True, timeout=15)

                    if result.returncode == 0:
                        print(f"[NOTIF] Sent via LTE ({iface}): {title}")
                        return True
                    else:
                        print(f"[NOTIF] LTE send failed: {result.stderr.decode()[:100]}")
                else:
                    print("[NOTIF] LTE modem not connected to cellular network")
            except Exception as e:
                print(f"[NOTIF] LTE error: {e}")

        print(f"[NOTIF] Failed to send: {title}")
        return False

    # =========================================================================
    # High-level notification methods
    # =========================================================================

    def notify_outage(self, soc: float, runtime_h: float):
        """Power outage detected."""
        if not self._enabled:
            return
        if not self._can_send("outage"):
            return

        runtime_str = f"{runtime_h:.0f}h" if runtime_h and runtime_h > 0 else "?"
        self._send_ntfy(
            title="Coupure de courant detectee",
            message=(
                f"La batterie de secours a pris le relais.\n"
                f"SoC : {soc:.0f}%\n"
                f"Autonomie estimee : {runtime_str}"
            ),
            priority=self._priority_map.get("outage", "high"),
            tags="zap,warning",
        )

    def notify_power_restored(self, duration_min: float, soc: float):
        """Power restored after outage."""
        if not self._enabled:
            return
        # Always send restore notification (no cooldown)
        with self._lock:
            self._cooldowns.pop("outage", None)

        hours = int(duration_min // 60)
        mins = int(duration_min % 60)
        duration_str = f"{hours}h{mins:02d}" if hours > 0 else f"{mins}min"

        self._send_ntfy(
            title="Courant retabli",
            message=(
                f"Le courant est revenu apres {duration_str}.\n"
                f"SoC batterie : {soc:.0f}%\n"
                f"La batterie va se recharger."
            ),
            priority=self._priority_map.get("info", "default"),
            tags="white_check_mark,zap",
        )

    def notify_level_change(self, level_name: str, soc: float,
                            runtime_h: float):
        """Pump intensity level changed."""
        if not self._enabled:
            return
        if not self._can_send(f"level_{level_name}"):
            return

        runtime_str = f"{runtime_h:.0f}h" if runtime_h and runtime_h > 0 else "?"
        tag_map = {"eco": "yellow_circle", "survival": "orange_circle",
                   "critical": "red_circle", "minimum": "black_circle",
                   "emergency": "red_circle"}
        tag = tag_map.get(level_name, "battery")

        self._send_ntfy(
            title=f"Niveau {level_name} active",
            message=(
                f"SoC batterie : {soc:.0f}%\n"
                f"Autonomie estimee : {runtime_str}\n"
                f"Les pompes ont ete ajustees."
            ),
            priority=self._priority_map.get("outage", "high"),
            tags=f"{tag},warning",
        )

    def notify_soc_critical(self, soc: float, runtime_h: float):
        """Battery critically low — repeated alert."""
        if not self._enabled:
            return
        # Use a shorter cooldown for critical alerts (60s)
        now = time.monotonic()
        with self._lock:
            last = self._cooldowns.get("critical", 0)
            if now - last < 60:
                return
            self._cooldowns["critical"] = now

        runtime_str = f"{runtime_h:.0f}h" if runtime_h and runtime_h > 0 else "< 1h"
        self._send_ntfy(
            title="BATTERIE CRITIQUE",
            message=(
                f"SoC a {soc:.0f}% !\n"
                f"Autonomie restante : {runtime_str}\n"
                f"Intervention urgente requise."
            ),
            priority=self._priority_map.get("critical", "urgent"),
            tags="rotating_light,sos",
        )

    def notify_network_failover(self, mode: str):
        """Network failover status change."""
        if not self._enabled:
            return
        if not self._can_send(f"net_{mode}"):
            return

        mode_labels = {
            "hotspot": "Hotspot Wi-Fi active",
            "rejoin": "Reconnexion Wi-Fi",
            "client": "Reseau normal retabli",
        }
        title = mode_labels.get(mode, f"Reseau: {mode}")

        self._send_ntfy(
            title=title,
            message=f"Mode reseau : {mode}",
            priority=self._priority_map.get("info", "default"),
            tags="satellite,wifi",
        )


# =============================================================================
# Factory
# =============================================================================

def create_notifier(cfg: dict) -> Notifier:
    """Create the notifier from config."""
    return Notifier(cfg)
