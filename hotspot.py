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
        self._accept_mac_file = "/tmp/reef_hostapd_accept.maclist"
        self._nat_active = False
        self._lock = threading.Lock()
        self._wifi_cut_thread: Optional[threading.Thread] = None

    def cut_wifi_for(self, minutes: float) -> None:
        """
        TEST helper: bring the Wi-Fi interface down for `minutes`, then
        back up, in a background thread. Lets the test blueprint observe
        the failover chain (client -> rejoin -> hotspot) and notification
        path against a real link loss.

        A guaranteed restore runs in a finally block even if anything
        throws, so the link can never stay down because of a test. A
        second request while one is active is ignored.
        """
        if minutes <= 0:
            return
        if (self._wifi_cut_thread is not None
                and self._wifi_cut_thread.is_alive()):
            print("[TEST] Wi-Fi cut already running, ignoring new request")
            return

        def _worker():
            iface = self._interface
            print(f"[TEST] Cutting Wi-Fi ({iface}) for {minutes:.0f} min")
            try:
                subprocess.run(
                    ["sudo", "ip", "link", "set", iface, "down"],
                    capture_output=True, timeout=10, check=False,
                )
                time.sleep(minutes * 60.0)
            finally:
                # Always bring it back, even on error/interrupt.
                subprocess.run(
                    ["sudo", "ip", "link", "set", iface, "up"],
                    capture_output=True, timeout=10, check=False,
                )
                print(f"[TEST] Wi-Fi ({iface}) restored")

        self._wifi_cut_thread = threading.Thread(target=_worker, daemon=True)
        self._wifi_cut_thread.start()

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

    def _count_reachable(self, controllers: list) -> int:
        """
        Count how many distinct controller devices respond to ping.

        Dedupes by IP so a multi-pump box (two logical controllers sharing
        one IP) counts as one device, matching the n/total progress logs.
        """
        reachable_ips = set()
        checked = set()
        for ctrl in controllers:
            ip = ctrl.get("ip")
            if not ip or ip in checked:
                continue
            checked.add(ip)
            if self.ping(ip):
                reachable_ips.add(ip)
        return len(reachable_ips)

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

                # MAC whitelist: only let our known ReefBeat devices
                # associate. Without this, the hotspot mirrors the home
                # SSID, so EVERY Wi-Fi device in the house (phones, feeders,
                # other ESP gadgets...) tries to join, saturates the RPi's
                # radio, and our pumps get kicked before they can even get
                # a DHCP lease. We saw 15 clients fighting over the AP.
                #
                # If no MACs are configured yet (fresh install), stay open
                # (macaddr_acl=0) so we don't lock everything out.
                mac_ips = self._hotspot_cfg.get("controller_mac_ips", {})
                allowed_macs = [m.strip().lower() for m in mac_ips.keys()
                                if m and m.strip()]

                if allowed_macs:
                    Path(self._accept_mac_file).write_text(
                        "\n".join(allowed_macs) + "\n")
                    acl_lines = (
                        f"macaddr_acl=1\n"
                        f"accept_mac_file={self._accept_mac_file}\n"
                    )
                    print(f"[HOTSPOT] MAC whitelist active "
                          f"({len(allowed_macs)} device(s) allowed)")
                else:
                    acl_lines = "macaddr_acl=0\n"
                    print("[HOTSPOT] No MAC whitelist configured — open AP "
                          "(run configure.py to restrict to your pumps)")

                # hostapd config
                hostapd = (
                    f"interface={self._interface}\n"
                    f"driver=nl80211\n"
                    f"ssid={ssid}\n"
                    f"hw_mode=g\n"
                    f"channel={channel}\n"
                    f"wmm_enabled=0\n"
                    f"{acl_lines}"
                    f"auth_algs=1\n"
                    f"ignore_broadcast_ssid=0\n"
                    f"wpa=2\n"
                    f"wpa_passphrase={password}\n"
                    f"wpa_key_mgmt=WPA-PSK\n"
                    f"wpa_pairwise=TKIP\n"
                    f"rsn_pairwise=CCMP\n"
                )
                Path(self._hostapd_conf).write_text(hostapd)

                # dnsmasq config.
                #
                # The hostapd MAC ACL rejects unknown devices at the 802.11
                # layer, but they still fire a DHCPDISCOVER before being
                # kicked, and dnsmasq (which doesn't know the ACL) would
                # answer and burn a lease. To stop that, we tag each known
                # MAC ("known") and tell dnsmasq to ignore any client that
                # is NOT tagged known. Result: only our pumps ever get a
                # lease, no residual leases from feeders/ESPs/etc.
                mac_reservations = self._hotspot_cfg.get(
                    "controller_mac_ips", {})

                dnsmasq = (
                    f"interface={self._interface}\n"
                    f"dhcp-range={dhcp_start},{dhcp_end},255.255.255.0,24h\n"
                    f"dhcp-leasefile=/tmp/reef_dnsmasq.leases\n"
                    f"bind-interfaces\n"
                    f"server=8.8.8.8\n"
                    f"domain-needed\n"
                    f"bogus-priv\n"
                )
                if mac_reservations:
                    # Only serve DHCP to known MACs.
                    dnsmasq += "dhcp-ignore=tag:!known\n"
                    for mac, ctrl_ip in mac_reservations.items():
                        # Tag this MAC "known" AND pin its reserved IP.
                        dnsmasq += f"dhcp-host={mac},set:known,{ctrl_ip}\n"
                Path(self._dnsmasq_conf).write_text(dnsmasq)

                # Purge any stale lease file from a previous hotspot session.
                # It persists across activations and would otherwise show
                # (and potentially reuse) leases from devices that are no
                # longer allowed, polluting the [DHCP] logs and the remap.
                try:
                    Path("/tmp/reef_dnsmasq.leases").unlink(missing_ok=True)
                except OSError:
                    pass

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
            "controller_reconnect_timeout_s", 900.0)

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

        # Wait for controllers to reconnect to our hotspot.
        #
        # Some devices (e.g. RSRUN) only re-scan and rejoin after their own
        # Wi-Fi watchdog fires, which can be several minutes — far longer
        # than the others. So we wait up to controller_reconnect_timeout_s
        # (default raised to 900s = 15 min to cover a typical device
        # watchdog), but return as soon as ALL controllers are reachable.
        # We log progress (n/total) so a slow device is visible.
        print(f"[FAILOVER] Level 3 — Waiting for controllers "
              f"(up to {reconnect_timeout:.0f}s)...")
        start = time.monotonic()
        total = len({c.get("ip") for c in controllers if c.get("ip")})
        last_count = -1
        while time.monotonic() - start < reconnect_timeout:
            if stop_event.is_set():
                return False
            time.sleep(5)

            # Check connected DHCP clients
            self._log_dhcp_clients()

            # Devices get NEW IPs on the hotspot subnet. Remap each
            # controller to the IP it actually obtained (from DHCP leases)
            # before testing reachability, otherwise we'd ping the old
            # 192.168.0.x addresses that no longer exist here.
            self.remap_controller_ips_from_leases(controllers)

            reachable = self._count_reachable(controllers)
            if reachable != last_count:
                elapsed = time.monotonic() - start
                print(f"[FAILOVER] Level 3 — {reachable}/{total} "
                      f"controller(s) reachable ({elapsed:.0f}s elapsed)")
                last_count = reachable

            if reachable >= total:
                print("[FAILOVER] Level 3 OK — "
                      "All controllers connected to hotspot")
                return True

        # Timed out: return True if at least one came back (partial success),
        # the slow ones will be picked up by the periodic health check.
        final = self._count_reachable(controllers)
        if final > 0:
            print(f"[FAILOVER] Level 3 — {final}/{total} reachable at timeout; "
                  "remaining devices will be retried by the health check")
            return True
        print("[FAILOVER] Level 3 — No controllers reconnected")
        return False

    def _log_dhcp_clients(self):
        """
        Log DHCP clients from OUR hotspot lease file.

        We read /tmp/reef_dnsmasq.leases (the file our dnsmasq instance
        writes), not the system default, and — when a MAC whitelist is in
        effect — only show allowed devices. This avoids logging stale or
        non-whitelisted entries that would suggest rogue clients got an IP
        when they did not.
        """
        try:
            allowed = {m.strip().lower() for m in
                       self._hotspot_cfg.get("controller_mac_ips", {}).keys()
                       if m and m.strip()}
            leases = Path("/tmp/reef_dnsmasq.leases")
            if not leases.exists():
                return
            for line in leases.read_text().strip().split("\n"):
                parts = line.split()
                if len(parts) >= 4:
                    mac, ip, name = parts[1].lower(), parts[2], parts[3]
                    if allowed and mac not in allowed:
                        continue  # not one of our pumps; don't show it
                    print(f"  [DHCP] {name} ({mac}) -> {ip}")
        except Exception:
            pass

    def _read_dhcp_leases(self) -> Dict[str, str]:
        """
        Parse the dnsmasq lease file into a {hostname: ip} map.

        Lease line format: <expiry> <mac> <ip> <hostname> <client-id>.
        Hostnames with value "*" (unknown) are skipped.
        """
        mapping: Dict[str, str] = {}
        for path in ("/var/lib/misc/dnsmasq.leases",
                     "/tmp/reef_dnsmasq.leases"):
            p = Path(path)
            if not p.exists():
                continue
            try:
                for line in p.read_text().strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] != "*":
                        mapping[parts[3]] = parts[2]
            except OSError:
                continue
        return mapping

    def _read_dhcp_leases_by_mac(self) -> Dict[str, str]:
        """
        Parse the dnsmasq lease file into a {mac: ip} map (MAC lowercased).

        This is the reliable cross-reference: ReefBeat DHCP hostnames don't
        always match our controller keys (e.g. an RSRUN can announce
        "SBS50_xxx"), but the MAC always matches what we collected during
        configuration.
        """
        mapping: Dict[str, str] = {}
        for path in ("/var/lib/misc/dnsmasq.leases",
                     "/tmp/reef_dnsmasq.leases"):
            p = Path(path)
            if not p.exists():
                continue
            try:
                for line in p.read_text().strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 3:
                        mapping[parts[1].lower()] = parts[2]
            except OSError:
                continue
        return mapping

    def remap_controller_ips_from_leases(self, controllers: list) -> int:
        """
        While the hotspot is active, the ReefBeat devices live on the
        hotspot subnet (192.168.4.0/24) instead of the home LAN
        (192.168.0.0/24). This rewrites each controller's live "ip" field
        to its hotspot address so pings and HTTP commands reach them.

        Strategies, in order of reliability:
          1. Real DHCP lease matched by MAC: the device's MAC (from the
             configured controller_mac_ips) is looked up in the live lease
             table, giving the IP it ACTUALLY obtained. This is ground
             truth and handles devices that didn't get their reservation.
          2. Deterministic octet substitution: keep the same last octet on
             the hotspot subnet (192.168.0.83 -> 192.168.4.83). Used when
             we have no MAC or no lease yet, and matches the dhcp-host
             reservations set up during configuration.
          3. Fallback by DHCP lease hostname (best-effort).

        The original IP is saved in "ip_original" so restore_controller_ips
        can put it back when we leave hotspot mode.
        """
        hotspot_prefix = ".".join(
            self._hotspot_cfg.get("ip", "192.168.4.1").split(".")[:3])

        leases = self._read_dhcp_leases()
        leases_by_mac = self._read_dhcp_leases_by_mac()

        # Build {reserved_hotspot_ip: mac} so we can find a controller's MAC
        # from its expected hotspot IP (controller_mac_ips is {mac: ip}).
        ip_to_mac = {ip: mac.lower() for mac, ip
                     in self._hotspot_cfg.get("controller_mac_ips", {}).items()}

        remapped = 0
        for ctrl in controllers:
            cur = ctrl.get("ip", "")
            # Use the ORIGINAL LAN ip as the substitution source, so calling
            # this repeatedly (e.g. each health check) is idempotent.
            src = ctrl.get("ip_original", cur)
            parts = src.split(".")

            new_ip = None

            # Strategy 2 value (computed first, used as the key to find MAC).
            subst_ip = f"{hotspot_prefix}.{parts[3]}" if len(parts) == 4 else None

            # Strategy 1: real lease by MAC (ground truth).
            mac = ip_to_mac.get(subst_ip) if subst_ip else None
            if mac and mac in leases_by_mac:
                new_ip = leases_by_mac[mac]

            # Strategy 2: deterministic octet substitution.
            if new_ip is None:
                new_ip = subst_ip

            # Strategy 3: fallback to lease hostname match.
            if new_ip is None and leases:
                host = ctrl.get("key", "").split(" / ")[0].strip()
                new_ip = leases.get(host)
                if new_ip is None:
                    for lh, lip in leases.items():
                        if host and (host in lh or lh in host):
                            new_ip = lip
                            break

            if new_ip and new_ip != ctrl.get("ip"):
                if "ip_original" not in ctrl:
                    ctrl["ip_original"] = cur
                print(f"  [NET] Remap {ctrl.get('key')}: "
                      f"{ctrl.get('ip')} -> {new_ip} (hotspot)")
                ctrl["ip"] = new_ip
                remapped += 1

        if remapped:
            print(f"[NET] Remapped {remapped} controller IP(s) to hotspot subnet")
        return remapped

    def restore_controller_ips(self, controllers: list) -> None:
        """
        Put back each controller's original LAN IP after leaving hotspot
        mode (saved in ip_original by remap_controller_ips_from_leases).
        """
        for ctrl in controllers:
            orig = ctrl.pop("ip_original", None)
            if orig and orig != ctrl.get("ip"):
                print(f"  [NET] Restore {ctrl.get('key')}: "
                      f"{ctrl.get('ip')} -> {orig}")
                ctrl["ip"] = orig

    # =========================================================================
    # Restore after power return
    # =========================================================================

    def restore_normal(self, controllers: Optional[list] = None):
        """
        Restore normal network mode after power returns.
        If hotspot was active, deactivate it.
        If we rejoined wifi, nothing to do (already connected).

        `controllers` (if given) have their original LAN IPs restored,
        undoing any hotspot-subnet remapping.
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

        # Always undo any hotspot IP remapping so we go back to the
        # configured LAN addresses.
        if controllers:
            self.restore_controller_ips(controllers)

    def cleanup(self):
        if self.mode == NetworkMode.HOTSPOT:
            self.deactivate_hotspot()
