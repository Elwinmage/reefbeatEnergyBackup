"""
Network manager with 3-level failover.

Level 1: Direct reach — controllers still reachable on current network
Level 2: Wi-Fi scan  — find and connect to the network the ReefBeats
         are on (e.g. router on UPS still alive but RPi lost connection)
Level 3: Mirror AP   — create hotspot with same SSID/password so
         ReefBeat controllers auto-reconnect to RPi

System requirements:
  sudo apt install hostapd dnsmasq wireless-tools
  sudo systemctl disable hostapd
  sudo systemctl disable dnsmasq
"""

import subprocess
import time
import re
import threading
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict


class NetworkMode(Enum):
    CLIENT = "client"           # Connected to home router normally
    CLIENT_REJOIN = "rejoin"    # Re-joined the home wifi after scan
    HOTSPOT = "hotspot"         # RPi is the access point
    UNKNOWN = "unknown"


class NetworkManager:
    """
    Manages network connectivity with 3-level failover.
    
    Level 1 — Direct reach:
      Controllers respond on current network. No action needed.
    
    Level 2 — Wi-Fi scan & connect:
      RPi lost network but home router may still be alive (on UPS).
      Scan for the home SSID, if found -> connect to it.
      Controllers are still connected to the router, so once RPi
      joins the same network, it can reach them again.
    
    Level 3 — Mirror hotspot:
      Home router is completely down. Create a hotspot with the
      same SSID and password. ReefBeat controllers will auto-
      reconnect since they already know the credentials.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._failover_cfg = cfg.get("failover", {})
        self._hotspot_cfg = cfg.get("hotspot", {})
        self._home_wifi = cfg.get("home_wifi", {})
        self._interface = cfg.get("interface", "wlan0")
        self._lte_gateway = cfg.get("lte_gateway", {})
        self.mode = NetworkMode.CLIENT
        self._hostapd_conf = "/tmp/reef_hostapd.conf"
        self._dnsmasq_conf = "/tmp/reef_dnsmasq.conf"
        self._nat_active = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._failover_cfg.get("enabled", False)

    @property
    def ssid(self) -> str:
        return self._home_wifi.get("ssid", "")

    @property
    def password(self) -> str:
        return self._home_wifi.get("password", "")

    # =========================================================================
    # Network utilities
    # =========================================================================

    def ping(self, ip: str, timeout: float = 2.0) -> bool:
        """Check if a host is reachable via ping."""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(int(timeout)), ip],
                capture_output=True, timeout=timeout + 1,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            return False

    def are_controllers_reachable(self, controllers: list) -> bool:
        """Check if at least one ReefBeat controller responds to ping."""
        for ctrl in controllers:
            ip = ctrl.get("ip")
            if ip and self.ping(ip):
                print(f"  [NET] {ctrl.get('name', ip)} reachable at {ip}")
                return True
        print("  [NET] No controllers reachable")
        return False

    def get_current_ssid(self) -> Optional[str]:
        """Get the SSID the RPi is currently connected to."""
        try:
            result = subprocess.run(
                ["iwgetid", "-r", self._interface],
                capture_output=True, text=True, timeout=5,
            )
            ssid = result.stdout.strip()
            return ssid if ssid else None
        except Exception:
            return None

    def scan_wifi_networks(self) -> List[Dict]:
        """
        Scan for available Wi-Fi networks.
        Returns list of dicts with 'ssid', 'signal', 'bssid'.
        """
        timeout = self._failover_cfg.get("scan_timeout_s", 15.0)
        try:
            # Bring interface up for scanning
            subprocess.run(
                ["sudo", "ip", "link", "set", self._interface, "up"],
                capture_output=True, timeout=5,
            )
            time.sleep(1)

            result = subprocess.run(
                ["sudo", "iwlist", self._interface, "scan"],
                capture_output=True, text=True, timeout=timeout,
            )

            if result.returncode != 0:
                print(f"[WIFI] Scan failed: {result.stderr.strip()}")
                return []

            networks = []
            current = {}
            for line in result.stdout.split("\n"):
                line = line.strip()

                # New cell
                if "Cell" in line and "Address:" in line:
                    if current.get("ssid"):
                        networks.append(current)
                    match = re.search(r"Address:\s*(\S+)", line)
                    current = {"bssid": match.group(1) if match else ""}

                # SSID
                elif "ESSID:" in line:
                    match = re.search(r'ESSID:"(.+)"', line)
                    if match:
                        current["ssid"] = match.group(1)

                # Signal level
                elif "Signal level" in line:
                    match = re.search(r"Signal level[=:](-?\d+)", line)
                    if match:
                        current["signal"] = int(match.group(1))

            # Don't forget last cell
            if current.get("ssid"):
                networks.append(current)

            print(f"[WIFI] Scan found {len(networks)} networks")
            for net in networks:
                sig = net.get("signal", "?")
                print(f"  [WIFI]   '{net['ssid']}' signal={sig}dBm")

            return networks

        except subprocess.TimeoutExpired:
            print("[WIFI] Scan timeout")
            return []
        except Exception as e:
            print(f"[WIFI] Scan error: {e}")
            return []

    def is_ssid_available(self, target_ssid: str) -> bool:
        """Scan and check if a specific SSID is visible."""
        networks = self.scan_wifi_networks()
        return any(n.get("ssid") == target_ssid for n in networks)

    # =========================================================================
    # Level 2: Connect to existing Wi-Fi
    # =========================================================================

    def connect_to_wifi(self, ssid: str, password: str) -> bool:
        """
        Connect the RPi to a specific Wi-Fi network.
        Uses wpa_supplicant for WPA2 networks.
        """
        timeout = self._failover_cfg.get("connect_timeout_s", 20.0)
        print(f"[WIFI] Connecting to '{ssid}'...")

        try:
            # Generate wpa_supplicant config for this network
            wpa_conf = "/tmp/reef_wpa.conf"
            result = subprocess.run(
                ["wpa_passphrase", ssid, password],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                print(f"[WIFI] wpa_passphrase failed: {result.stderr}")
                return False

            # Add country and ctrl_interface to config
            wpa_content = (
                "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
                "update_config=1\n"
                "country=FR\n"
                + result.stdout
            )
            Path(wpa_conf).write_text(wpa_content)

            # Stop any existing wpa_supplicant
            subprocess.run(
                ["sudo", "killall", "wpa_supplicant"],
                capture_output=True)
            time.sleep(1)

            # Flush old IP
            subprocess.run(
                ["sudo", "ip", "addr", "flush", "dev", self._interface],
                capture_output=True)

            # Start wpa_supplicant
            subprocess.run([
                "sudo", "wpa_supplicant",
                "-B",                           # Background
                "-i", self._interface,
                "-c", wpa_conf,
            ], capture_output=True, check=True)

            # Get IP via DHCP
            subprocess.run(
                ["sudo", "dhclient", "-v", self._interface],
                capture_output=True, timeout=timeout)

            # Verify connection
            time.sleep(3)
            current = self.get_current_ssid()
            if current == ssid:
                print(f"[WIFI] Connected to '{ssid}' successfully")
                return True
            else:
                print(f"[WIFI] Connection failed (current SSID: {current})")
                return False

        except subprocess.TimeoutExpired:
            print("[WIFI] Connection timeout")
            return False
        except Exception as e:
            print(f"[WIFI] Connection error: {e}")
            return False

    # =========================================================================
    # Level 3: Mirror hotspot
    # =========================================================================

    def activate_hotspot(self) -> bool:
        """Create AP with same SSID/password as home network."""
        with self._lock:
            if self.mode == NetworkMode.HOTSPOT:
                print("[HOTSPOT] Already active")
                return True

            print("[HOTSPOT] Activating mirror AP...")
            ssid = self.ssid
            password = self.password
            ip = self._hotspot_cfg.get("ip", "192.168.4.1")
            channel = self._hotspot_cfg.get("channel", 6)
            dhcp_start = self._hotspot_cfg.get("dhcp_start", "192.168.4.10")
            dhcp_end = self._hotspot_cfg.get("dhcp_end", "192.168.4.50")

            try:
                # Stop client mode
                subprocess.run(
                    ["sudo", "systemctl", "stop", "wpa_supplicant"],
                    capture_output=True)
                subprocess.run(
                    ["sudo", "killall", "wpa_supplicant"],
                    capture_output=True)
                time.sleep(1)

                # Static IP
                subprocess.run(
                    ["sudo", "ip", "addr", "flush", "dev", self._interface],
                    capture_output=True)
                subprocess.run(
                    ["sudo", "ip", "addr", "add", f"{ip}/24",
                     "dev", self._interface],
                    capture_output=True)
                subprocess.run(
                    ["sudo", "ip", "link", "set", self._interface, "up"],
                    capture_output=True)

                # hostapd config
                hostapd = (
                    f"interface={self._interface}\n"
                    f"driver=nl80211\n"
                    f"ssid={ssid}\n"
                    f"hw_mode=g\n"
                    f"channel={channel}\n"
                    f"wmm_enabled=0\n"
                    f"macaddr_acl=0\n"
                    f"auth_algs=1\n"
                    f"ignore_broadcast_ssid=0\n"
                    f"wpa=2\n"
                    f"wpa_passphrase={password}\n"
                    f"wpa_key_mgmt=WPA-PSK\n"
                    f"wpa_pairwise=TKIP\n"
                    f"rsn_pairwise=CCMP\n"
                )
                Path(self._hostapd_conf).write_text(hostapd)

                # dnsmasq config
                dnsmasq = (
                    f"interface={self._interface}\n"
                    f"dhcp-range={dhcp_start},{dhcp_end},255.255.255.0,24h\n"
                    f"bind-interfaces\n"
                    f"server=8.8.8.8\n"
                    f"domain-needed\n"
                    f"bogus-priv\n"
                )
                for mac, ctrl_ip in self._hotspot_cfg.get(
                        "controller_mac_ips", {}).items():
                    dnsmasq += f"dhcp-host={mac},{ctrl_ip}\n"
                Path(self._dnsmasq_conf).write_text(dnsmasq)

                # Start services
                subprocess.run(
                    ["sudo", "killall", "dnsmasq"], capture_output=True)
                subprocess.run(
                    ["sudo", "dnsmasq", f"--conf-file={self._dnsmasq_conf}"],
                    capture_output=True, check=True)

                result = subprocess.run(
                    ["sudo", "hostapd", "-B", self._hostapd_conf],
                    capture_output=True)

                if result.returncode == 0:
                    self.mode = NetworkMode.HOTSPOT
                    print(f"[HOTSPOT] Active: SSID='{ssid}' IP={ip}")

                    # Enable LTE gateway (NAT) if configured
                    if self._lte_gateway.get("enabled", False):
                        self._enable_lte_nat()

                    return True
                else:
                    err = result.stderr.decode()
                    print(f"[HOTSPOT] hostapd failed: {err}")
                    self.deactivate_hotspot()
                    return False

            except Exception as e:
                print(f"[HOTSPOT] Error: {e}")
                self.deactivate_hotspot()
                return False

    def deactivate_hotspot(self) -> bool:
        """Stop AP, disable NAT, and restore client mode."""
        with self._lock:
            print("[HOTSPOT] Deactivating...")
            try:
                # Disable NAT first
                if self._nat_active:
                    self._disable_lte_nat()

                subprocess.run(
                    ["sudo", "killall", "hostapd"], capture_output=True)
                subprocess.run(
                    ["sudo", "killall", "dnsmasq"], capture_output=True)
                time.sleep(1)

                subprocess.run(
                    ["sudo", "ip", "addr", "flush", "dev", self._interface],
                    capture_output=True)
                subprocess.run(
                    ["sudo", "systemctl", "restart", "wpa_supplicant"],
                    capture_output=True)
                subprocess.run(
                    ["sudo", "systemctl", "restart", "dhcpcd"],
                    capture_output=True)

                self.mode = NetworkMode.CLIENT
                print("[HOTSPOT] Deactivated, client mode restored")
                time.sleep(5)
                return True

            except Exception as e:
                print(f"[HOTSPOT] Deactivation error: {e}")
                self.mode = NetworkMode.UNKNOWN
                return False

    # =========================================================================
    # LTE NAT gateway — route hotspot traffic through 4G modem
    # =========================================================================

    def _detect_lte_interface(self) -> Optional[str]:
        """Find the LTE modem network interface."""
        # Check configured interface first
        configured = self._lte_gateway.get("interface", "auto")
        if configured != "auto":
            try:
                result = subprocess.run(
                    ["ip", "link", "show", configured],
                    capture_output=True, timeout=3)
                if result.returncode == 0:
                    return configured
            except Exception:
                pass

        # Auto-detect: look for HiLink gateway in routing table
        try:
            result = subprocess.run(
                ["ip", "route"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split("\n"):
                if "192.168.8.1" in line:
                    match = re.search(r'dev\s+(\S+)', line)
                    if match:
                        return match.group(1)
        except Exception:
            pass

        # Fallback: check common interface names
        for iface in ["eth1", "enx", "usb0", "wwan0"]:
            try:
                result = subprocess.run(
                    ["ip", "link", "show", iface],
                    capture_output=True, timeout=3)
                if result.returncode == 0:
                    return iface
            except Exception:
                pass

        return None

    def _enable_lte_nat(self):
        """
        Enable NAT routing from hotspot (wlan0) to LTE modem (eth1).
        
        This allows ReefBeat devices connected to the RPi hotspot to
        reach the Red Sea cloud servers via the 4G modem, keeping the
        mobile app functional even when the home router is down.
        """
        lte_iface = self._detect_lte_interface()
        if not lte_iface:
            print("[NAT] No LTE interface found, skipping gateway setup")
            return

        ap_iface = self._interface
        print(f"[NAT] Enabling gateway: {ap_iface} → {lte_iface}")

        try:
            # Enable IP forwarding
            subprocess.run(
                ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
                capture_output=True, check=True)

            # NAT: masquerade outgoing traffic on LTE interface
            subprocess.run([
                "sudo", "iptables", "-t", "nat", "-A", "POSTROUTING",
                "-o", lte_iface, "-j", "MASQUERADE"
            ], capture_output=True, check=True)

            # Allow forwarding from hotspot to LTE
            subprocess.run([
                "sudo", "iptables", "-A", "FORWARD",
                "-i", ap_iface, "-o", lte_iface, "-j", "ACCEPT"
            ], capture_output=True, check=True)

            # Allow established/related return traffic
            subprocess.run([
                "sudo", "iptables", "-A", "FORWARD",
                "-i", lte_iface, "-o", ap_iface,
                "-m", "state", "--state", "RELATED,ESTABLISHED",
                "-j", "ACCEPT"
            ], capture_output=True, check=True)

            self._nat_active = True
            self._nat_lte_iface = lte_iface
            print(f"[NAT] Gateway active: ReefBeat devices can reach "
                  f"the internet via 4G ({lte_iface})")

        except subprocess.CalledProcessError as e:
            print(f"[NAT] Failed to enable: {e}")
        except Exception as e:
            print(f"[NAT] Error: {e}")

    def _disable_lte_nat(self):
        """Remove NAT rules and disable IP forwarding."""
        lte_iface = getattr(self, '_nat_lte_iface', None)
        ap_iface = self._interface

        print("[NAT] Disabling gateway...")
        try:
            if lte_iface:
                # Remove specific rules
                subprocess.run([
                    "sudo", "iptables", "-t", "nat", "-D", "POSTROUTING",
                    "-o", lte_iface, "-j", "MASQUERADE"
                ], capture_output=True)
                subprocess.run([
                    "sudo", "iptables", "-D", "FORWARD",
                    "-i", ap_iface, "-o", lte_iface, "-j", "ACCEPT"
                ], capture_output=True)
                subprocess.run([
                    "sudo", "iptables", "-D", "FORWARD",
                    "-i", lte_iface, "-o", ap_iface,
                    "-m", "state", "--state", "RELATED,ESTABLISHED",
                    "-j", "ACCEPT"
                ], capture_output=True)

            # Disable IP forwarding
            subprocess.run(
                ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=0"],
                capture_output=True)

            self._nat_active = False
            print("[NAT] Gateway disabled")

        except Exception as e:
            print(f"[NAT] Cleanup error: {e}")

    # =========================================================================
    # 3-level failover orchestration
    # =========================================================================

    def execute_failover(self, controllers: list,
                         stop_event: threading.Event) -> bool:
        """
        Execute the 3-level failover sequence.
        Returns True if controllers are reachable after failover.
        
        Level 1: Try to reach controllers directly
        Level 2: Scan wifi, find home SSID, connect to it
        Level 3: Create mirror hotspot
        """
        if not self.enabled:
            print("[FAILOVER] Disabled in config")
            return False

        retry_count = self._failover_cfg.get("retry_count", 3)
        retry_delay = self._failover_cfg.get("retry_delay_s", 5.0)
        reconnect_timeout = self._failover_cfg.get(
            "controller_reconnect_timeout_s", 60.0)

        # =================================================================
        # Level 1: Direct reach
        # =================================================================
        print("[FAILOVER] Level 1 — Checking direct connectivity...")

        for attempt in range(retry_count):
            if stop_event.is_set():
                return False
            print(f"  [L1] Attempt {attempt + 1}/{retry_count}")
            if self.are_controllers_reachable(controllers):
                print("[FAILOVER] Level 1 OK — Controllers reachable directly")
                return True
            if attempt < retry_count - 1:
                if stop_event.wait(timeout=retry_delay):
                    return False

        # =================================================================
        # Level 2: Scan and connect to home Wi-Fi
        # =================================================================
        print(f"[FAILOVER] Level 2 — Scanning for '{self.ssid}'...")

        if stop_event.is_set():
            return False

        if self.is_ssid_available(self.ssid):
            print(f"[FAILOVER] Level 2 — '{self.ssid}' found! Connecting...")

            if self.connect_to_wifi(self.ssid, self.password):
                self.mode = NetworkMode.CLIENT_REJOIN
                print("[FAILOVER] Level 2 — Connected, "
                      "waiting for controllers...")

                # Wait for controllers to become reachable
                start = time.monotonic()
                while time.monotonic() - start < reconnect_timeout:
                    if stop_event.is_set():
                        return False
                    if self.are_controllers_reachable(controllers):
                        print("[FAILOVER] Level 2 OK — "
                              "Controllers reachable via rejoin")
                        return True
                    time.sleep(5)

                print("[FAILOVER] Level 2 — Connected but "
                      "controllers not responding")
            else:
                print("[FAILOVER] Level 2 — Connection failed")
        else:
            print(f"[FAILOVER] Level 2 — '{self.ssid}' not found in scan")

        # =================================================================
        # Level 3: Mirror hotspot
        # =================================================================
        print("[FAILOVER] Level 3 — Creating mirror hotspot...")

        if stop_event.is_set():
            return False

        if not self.activate_hotspot():
            print("[FAILOVER] Level 3 — Hotspot activation failed")
            return False

        # Wait for controllers to reconnect to our hotspot
        print(f"[FAILOVER] Level 3 — Waiting for controllers "
              f"(up to {reconnect_timeout}s)...")
        start = time.monotonic()
        while time.monotonic() - start < reconnect_timeout:
            if stop_event.is_set():
                return False
            time.sleep(5)

            # Check connected DHCP clients
            self._log_dhcp_clients()

            if self.are_controllers_reachable(controllers):
                print("[FAILOVER] Level 3 OK — "
                      "Controllers connected to hotspot")
                return True

        print("[FAILOVER] Level 3 — Some controllers may not "
              "have reconnected")
        return False

    def _log_dhcp_clients(self):
        """Log currently connected DHCP clients for debugging."""
        try:
            leases = Path("/var/lib/misc/dnsmasq.leases")
            if leases.exists():
                content = leases.read_text().strip()
                if content:
                    for line in content.split("\n"):
                        parts = line.split()
                        if len(parts) >= 4:
                            mac, ip, name = parts[1], parts[2], parts[3]
                            print(f"  [DHCP] {name} ({mac}) -> {ip}")
        except Exception:
            pass

    # =========================================================================
    # Restore after power return
    # =========================================================================

    def restore_normal(self):
        """
        Restore normal network mode after power returns.
        If hotspot was active, deactivate it.
        If we rejoined wifi, nothing to do (already connected).
        """
        if self.mode == NetworkMode.HOTSPOT:
            print("[NET] Restoring from hotspot to client mode...")
            self.deactivate_hotspot()
            # Reconnect to home wifi
            time.sleep(3)
            self.connect_to_wifi(self.ssid, self.password)
            time.sleep(5)
        elif self.mode == NetworkMode.CLIENT_REJOIN:
            print("[NET] Already connected via rejoin, nothing to restore")
            self.mode = NetworkMode.CLIENT

    def cleanup(self):
        if self.mode == NetworkMode.HOTSPOT:
            self.deactivate_hotspot()
