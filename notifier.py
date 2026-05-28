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
  - LTE link periodic health check failed
  - Normal internet connectivity lost (alert sent via LTE)

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
      "connectivity": {
          "lte_check_interval_h": 8,
          "internet_check_interval_h": 8,
          "internet_check_host": "8.8.8.8",
          "internet_check_interfaces": ["eth0", "wlan0"]
      },
      "cooldown_s": 300
  }

Connectivity checks (driven by the main loop):
  - lte_check_interval_h:      test the LTE link every X hours (0 = off).
                               Notifies (via Wi-Fi/internet) if the LTE
                               link is down, so the failover path is known
                               to be healthy *before* a real outage.
  - internet_check_interval_h: test the normal internet link every X hours
                               (0 = off). If down, the alert is forced out
                               through the LTE modem.
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
    Detects and manages USB 4G/LTE connections.

    Supports two modes:
      - e3372h: Huawei E3372h HiLink USB modem (gateway 192.168.8.1)
      - tethering: USB tethering from a smartphone (usb0 or similar)
    """

    HILINK_GATEWAY = "192.168.8.1"
    HILINK_STATUS_URL = "http://192.168.8.1/api/monitoring/status"

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._mode = cfg.get("mode", "e3372h")  # e3372h or tethering
        self._interface: Optional[str] = None
        self._available = False

    def detect(self) -> bool:
        """Detect if a LTE connection is available."""
        if self._mode == "tethering":
            return self._detect_tethering()
        return self._detect_e3372h()

    def _detect_tethering(self) -> bool:
        """Detect USB tethering interface.
        
        USB tethering interfaces report "state UNKNOWN" (not "state UP")
        because Linux cannot query the physical link state of a USB
        gadget. We also accept LOWER_UP as a reliable indicator.
        """
        try:
            result = subprocess.run(
                ["ip", "link", "show"], capture_output=True, text=True, timeout=5
            )
            for iface in ["usb0", "usb1", "eth1", "enp0s"]:
                for line in result.stdout.split("\n"):
                    if iface in line and ("state UP" in line
                            or "state UNKNOWN" in line):
                        match = re.search(r'\d+:\s+(\S+):', line)
                        if match:
                            self._interface = match.group(1).rstrip(":")
                            self._available = True
                            print(f"[LTE] Tethering interface found: {self._interface}")
                            return True
        except Exception as e:
            print(f"[LTE] Tethering detection error: {e}")

        print("[LTE] No tethering interface found")
        return False

    def _detect_e3372h(self) -> bool:
        """Detect Huawei E3372h HiLink modem."""
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
        """Check if the LTE connection is active."""
        if not self._available:
            return False

        if self._mode == "tethering":
            # Tethering: just ping via the interface
            return self._ping_test()

        # E3372h: try HiLink API first
        if HAS_REQUESTS:
            try:
                r = requests.get(self.HILINK_STATUS_URL, timeout=3)
                if r.status_code == 200:
                    return "901" in r.text
            except Exception:
                pass

        # Fallback: ping test
        return self._ping_test()

    def _ping_test(self) -> bool:
        """Test internet connectivity via the LTE interface."""
        if not self._interface:
            return False
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "3", "-I", self._interface,
                 "8.8.8.8"],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
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

        # Periodic connectivity checks config (intervals in hours).
        self._conn_cfg = self._cfg.get("connectivity", {})

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

    def _send_via_lte(self, title: str, message: str,
                      priority: str = "default",
                      tags: str = "") -> bool:
        """Send a notification *forced* through the LTE modem.

        Used when the normal internet link is known to be down: skip the
        Wi-Fi attempt entirely and push straight out over the cellular
        interface with curl --interface.
        """
        if not (self._lte_enabled and self._lte and self._lte.available):
            print("[NOTIF] LTE not available, cannot force-send")
            return False

        url = f"{self._ntfy_server}/{self._ntfy_topic}"
        try:
            iface = self._lte.interface
            if not (iface and self._lte.is_connected()):
                print("[NOTIF] LTE modem not connected to cellular network")
                return False
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
            print(f"[NOTIF] LTE send failed: {result.stderr.decode()[:100]}")
        except Exception as e:
            print(f"[NOTIF] LTE error: {e}")
        return False

    # =========================================================================
    # Connectivity helpers
    # =========================================================================

    def lte_is_healthy(self) -> bool:
        """Return True if the LTE link is detected and connected to cellular.

        Re-runs detection each time (the USB modem may have been plugged in
        or dropped since startup) before checking the cellular connection.
        """
        if not (self._lte_enabled and self._lte):
            return False
        # detect() refreshes the interface; is_connected() checks the link.
        if not self._lte.detect():
            return False
        return self._lte.is_connected()

    @property
    def lte_enabled(self) -> bool:
        return self._lte_enabled

    def internet_is_up(self) -> bool:
        """Check normal internet connectivity over the WAN interfaces.

        We ping a public host *bound to each WAN interface in turn*
        (default: eth0 then wlan0) rather than over the default route.
        Internet is considered up as soon as one of them succeeds.

        Binding explicitly with -I matters: during a hotspot failover the
        4G modem may become the default route, so a plain ping would
        succeed over LTE and mask a real WAN outage. We only want to count
        the normal links here.

        If an interface in the list is absent/down, its ping just fails and
        we fall through to the next one. If none are reachable, the WAN is
        considered down.
        """
        host = self._conn_cfg.get("internet_check_host", "8.8.8.8")
        ifaces = self._conn_cfg.get("internet_check_interfaces",
                                    ["eth0", "wlan0"])
        for iface in ifaces:
            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "3", "-I", iface, host],
                    capture_output=True, timeout=6,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                # Interface missing, down, or ping error -> try the next one.
                continue
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

    # =========================================================================
    # Periodic connectivity check helpers + notifications
    # =========================================================================

    def lte_check_interval_h(self) -> float:
        """Interval (hours) for the periodic LTE health check. 0 = disabled.

        Defaults to 8h. The check is only meaningful when LTE failover is
        enabled, so it is forced to 0 (off) otherwise.
        """
        if not self._lte_enabled:
            return 0.0
        return float(self._conn_cfg.get("lte_check_interval_h", 8))

    def internet_check_interval_h(self) -> float:
        """Interval (hours) for the periodic internet check. 0 = disabled."""
        return float(self._conn_cfg.get("internet_check_interval_h", 8))

    def run_lte_check(self) -> bool:
        """Test the LTE link and notify if it is down.

        Returns the link health (True = healthy). The "down" notification
        goes out over the normal internet link (which is, by assumption,
        still up since we are only auditing the backup path here).
        """
        if not self._enabled or not self._lte_enabled:
            return False
        healthy = self.lte_is_healthy()
        if healthy:
            print("[CONN] LTE link OK")
            return True
        print("[CONN] LTE link DOWN")
        if not self._can_send("lte_down"):
            return False
        self._send_ntfy(
            title="Liaison 4G/LTE en panne",
            message=(
                "Le test periodique de la liaison 4G a echoue.\n"
                "Le failover par 4G ne fonctionnera pas en cas de coupure.\n"
                "Verifiez le modem USB / l'abonnement."
            ),
            priority=self._priority_map.get("outage", "high"),
            tags="signal_strength,warning",
        )
        return False

    def run_internet_check(self) -> bool:
        """Test the normal internet link and notify (via LTE) if it is down.

        Returns the link health (True = up). When down, the alert is forced
        through the LTE modem since the normal path obviously cannot carry
        it.
        """
        if not self._enabled:
            return False
        if self.internet_is_up():
            print("[CONN] Internet link OK")
            return True
        print("[CONN] Internet link DOWN")
        if not self._can_send("internet_down"):
            return False
        # Normal WAN is down: the only way out is the LTE modem.
        self._send_via_lte(
            title="Connexion internet perdue",
            message=(
                "La connexion internet normale ne repond plus.\n"
                "Cette alerte a ete envoyee via la 4G/LTE.\n"
                "Verifiez la box / le routeur."
            ),
            priority=self._priority_map.get("outage", "high"),
            tags="globe_with_meridians,warning",
        )
        return False


# =============================================================================
# Factory
# =============================================================================

def create_notifier(cfg: dict) -> Notifier:
    """Create the notifier from config."""
    return Notifier(cfg)
