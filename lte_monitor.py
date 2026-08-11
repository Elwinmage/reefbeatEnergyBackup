"""
reefbeat⚡Backup — LTE Monitor module.

Periodically checks 4G modem status and publishes telemetry to
Home Assistant via MQTT auto-discovery sensors.

Supports three modem types:
  - E3372h: queries HiLink HTTP API (192.168.8.1)
  - SIM7600: queries AT commands via serial (/dev/ttyUSB2)
  - Tethering: basic OS-level interface checks only

Published sensors (when available):
  - signal_strength (RSSI in dBm)
  - signal_quality (0-31 scale)
  - operator
  - network_type (4G/3G/2G)
  - sim_status (ready/pin_required/absent)
  - model
  - manufacturer
  - firmware
  - imei
  - ip_address
  - connected (binary: internet reachable)

Configuration (config.json):
  "lte_monitor": {
      "check_interval_min": 10,
  }
"""

import json
import re
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from device_info import build_device_info

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False


# =============================================================================
# LTE Monitor
# =============================================================================

class LteMonitor:
    """
    Background monitor for 4G modem status.

    Periodically queries the modem for signal, network, and SIM info,
    checks internet connectivity, and publishes everything to HA via
    MQTT auto-discovery.
    """

    def __init__(self, cfg: dict, mqtt_client):
        self._lte_cfg = cfg.get("notifications", {}).get("lte_failover", {})
        self._monitor_cfg = cfg.get("lte_monitor", {})
        self._mqtt_cfg = cfg.get("mqtt", {})
        self._client = mqtt_client

        self._mode = self._lte_cfg.get("mode", "none")
        self._enabled = (self._lte_cfg.get("enabled", False)
                         and self._mode != "none")
        self._interval = self._monitor_cfg.get("check_interval_min", 10) * 60

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._state: Dict[str, Any] = {}
        self._interface = self._lte_cfg.get("interface", "auto")

        # SIM7600 AT port
        self._at_port = self._lte_cfg.get("at_port", "/dev/ttyUSB2")
        self._apn = cfg.get("sim7600", {}).get("apn", "orange")

        if self._enabled:
            print(f"[LTE-MON] Mode: {self._mode}, "
                  f"check every {self._interval // 60} min")

    def start(self):
        """Start the background monitoring thread."""
        if not self._enabled:
            return
        self._publish_ha_discovery()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def state(self) -> Dict[str, Any]:
        """Current LTE state (read-only)."""
        return dict(self._state)

    # =========================================================================
    # Background loop
    # =========================================================================

    def _loop(self):
        """Periodic check loop."""
        # First check after 15s (let everything initialize)
        if self._stop.wait(timeout=15):
            return

        while not self._stop.is_set():
            try:
                self._check()
            except Exception as e:
                print(f"[LTE-MON] Check error: {e}")
            if self._stop.wait(timeout=self._interval):
                return

    def _check(self):
        """Run a single check cycle."""
        if self._mode == "e3372h":
            self._check_e3372h()
        elif self._mode == "sim7600":
            self._check_sim7600()
        elif self._mode == "tethering":
            self._check_tethering()

        # Internet connectivity test
        iface = self._detect_interface()
        if iface:
            self._state["interface"] = iface
            self._state["ip_address"] = self._get_ip(iface)
            self._state["connected"] = self._ping_test(iface)
        else:
            self._state["interface"] = "none"
            self._state["ip_address"] = ""
            self._state["connected"] = False

        self._publish_state()

    # =========================================================================
    # E3372h checks (HiLink HTTP API)
    # =========================================================================

    def _check_e3372h(self):
        """Query Huawei E3372h HiLink API."""
        if not HAS_REQUESTS:
            return

        base = "http://192.168.8.1"

        # Get session token first
        try:
            tok_r = requests.get(f"{base}/api/webserver/SesTokInfo", timeout=3)
            token = ""
            cookie = ""
            if tok_r.ok:
                ses = re.search(r'<SesInfo>(.+?)</SesInfo>', tok_r.text)
                tok = re.search(r'<TokInfo>(.+?)</TokInfo>', tok_r.text)
                if ses:
                    cookie = ses.group(1)
                if tok:
                    token = tok.group(1)
        except Exception:
            self._state["connected"] = False
            return

        headers = {"Cookie": cookie, "__RequestVerificationToken": token}

        # Device info
        try:
            r = requests.get(f"{base}/api/device/information", headers=headers, timeout=3)
            if r.ok:
                self._state["manufacturer"] = "Huawei"
                m = re.search(r'<DeviceName>(.+?)</DeviceName>', r.text)
                if m:
                    self._state["model"] = m.group(1)
                m = re.search(r'<Imei>(.+?)</Imei>', r.text)
                if m:
                    self._state["imei"] = m.group(1)
                m = re.search(r'<SoftwareVersion>(.+?)</SoftwareVersion>', r.text)
                if m:
                    self._state["firmware"] = m.group(1)
        except Exception:
            pass

        # Signal
        try:
            r = requests.get(f"{base}/api/device/signal", headers=headers, timeout=3)
            if r.ok:
                m = re.search(r'<rssi>(.+?)dBm</rssi>', r.text)
                if m:
                    self._state["signal_strength"] = int(m.group(1))
                    # Convert to 0-31 scale
                    dbm = int(m.group(1))
                    rssi = max(0, min(31, (dbm + 113) // 2))
                    self._state["signal_quality"] = rssi
        except Exception:
            pass

        # Network
        try:
            r = requests.get(f"{base}/api/monitoring/status", headers=headers, timeout=3)
            if r.ok:
                # Network type
                m = re.search(r'<CurrentNetworkType>(\d+)</CurrentNetworkType>', r.text)
                if m:
                    net_types = {
                        "0": "No service", "1": "GSM", "2": "GPRS", "3": "EDGE",
                        "4": "WCDMA", "5": "HSDPA", "6": "HSUPA", "7": "HSPA+",
                        "8": "TD-SCDMA", "9": "HSPA+", "19": "LTE",
                    }
                    self._state["network_type"] = net_types.get(
                        m.group(1), f"Unknown ({m.group(1)})")
                # SIM status
                m = re.search(r'<SimStatus>(\d+)</SimStatus>', r.text)
                if m:
                    sim_map = {"0": "absent", "1": "ready"}
                    self._state["sim_status"] = sim_map.get(m.group(1), "unknown")
        except Exception:
            pass

        # Operator (PLMN)
        try:
            r = requests.get(f"{base}/api/net/current-plmn", headers=headers, timeout=3)
            if r.ok:
                m = re.search(r'<FullName>(.+?)</FullName>', r.text)
                if m:
                    self._state["operator"] = m.group(1)
        except Exception:
            pass

    # =========================================================================
    # SIM7600 checks (AT commands via serial)
    # =========================================================================

    def _check_sim7600(self):
        """Query SIM7600G-H via AT commands."""
        if not HAS_SERIAL:
            return

        try:
            ser = serial.Serial(self._at_port, 115200, timeout=3)
        except Exception as e:
            print(f"[LTE-MON] Cannot open {self._at_port}: {e}")
            return

        try:
            # Manufacturer
            resp = self._at_cmd(ser, "AT+CGMI")
            if resp:
                self._state["manufacturer"] = self._clean_at(resp, "AT+CGMI")

            # Model
            resp = self._at_cmd(ser, "AT+CGMM")
            if resp:
                self._state["model"] = self._clean_at(resp, "AT+CGMM")

            # Firmware
            resp = self._at_cmd(ser, "AT+CGMR")
            if resp:
                fw = self._clean_at(resp, "AT+CGMR")
                # Remove "+CGMR: " prefix if present
                fw = re.sub(r'^\+CGMR:\s*', '', fw)
                self._state["firmware"] = fw.split("\n")[0].strip()

            # IMEI
            resp = self._at_cmd(ser, "AT+CGSN")
            if resp:
                imei = self._clean_at(resp, "AT+CGSN")
                self._state["imei"] = imei.split("\n")[0].strip()

            # SIM status
            resp = self._at_cmd(ser, "AT+CPIN?")
            if resp:
                if "READY" in resp:
                    self._state["sim_status"] = "ready"
                elif "SIM PIN" in resp:
                    self._state["sim_status"] = "pin_required"
                elif "SIM PUK" in resp:
                    self._state["sim_status"] = "puk_locked"
                elif "NOT INSERTED" in resp:
                    self._state["sim_status"] = "absent"
                else:
                    self._state["sim_status"] = "unknown"

            # Signal quality
            resp = self._at_cmd(ser, "AT+CSQ")
            if resp:
                m = re.search(r'\+CSQ:\s*(\d+),', resp)
                if m:
                    rssi = int(m.group(1))
                    self._state["signal_quality"] = rssi if rssi != 99 else 0
                    if rssi != 99:
                        self._state["signal_strength"] = -113 + 2 * rssi
                    else:
                        self._state["signal_strength"] = 0

            # Network registration
            resp = self._at_cmd(ser, "AT+CREG?")
            if resp:
                m = re.search(r'\+CREG:\s*\d+,(\d+)', resp)
                if m:
                    reg_map = {
                        "0": "not_registered", "1": "home",
                        "2": "searching", "3": "denied",
                        "5": "roaming",
                    }
                    self._state["registration"] = reg_map.get(
                        m.group(1), "unknown")

            # Operator
            resp = self._at_cmd(ser, "AT+COPS?")
            if resp:
                m = re.search(r'\+COPS:\s*\d+,\d+,"(.+?)"', resp)
                if m:
                    self._state["operator"] = m.group(1)

            # Network type (access technology)
            resp = self._at_cmd(ser, "AT+CNSMOD?")
            if resp:
                m = re.search(r'\+CNSMOD:\s*\d+,(\d+)', resp)
                if m:
                    mode_map = {
                        "0": "No service", "1": "GSM", "2": "GPRS",
                        "3": "EDGE", "4": "WCDMA", "5": "HSDPA",
                        "6": "HSUPA", "7": "HSPA+", "8": "LTE",
                    }
                    self._state["network_type"] = mode_map.get(
                        m.group(1), f"Unknown ({m.group(1)})")

            # IP address from PDP context
            resp = self._at_cmd(ser, "AT+CGPADDR=1")
            if resp:
                m = re.search(r'\+CGPADDR:\s*1,(\d+\.\d+\.\d+\.\d+)', resp)
                if m:
                    self._state["modem_ip"] = m.group(1)

        finally:
            ser.close()

    def _at_cmd(self, ser, cmd: str, wait: float = 1.0) -> str:
        """Send an AT command and return the response."""
        ser.reset_input_buffer()
        ser.write((cmd + "\r\n").encode())
        time.sleep(wait)
        resp = ""
        while ser.in_waiting > 0:
            resp += ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            time.sleep(0.1)
        return resp.strip()

    def _clean_at(self, resp: str, cmd: str) -> str:
        """Remove the echoed command and OK from AT response."""
        return resp.replace(cmd, "").replace("OK", "").strip()

    # =========================================================================
    # Tethering checks (OS-level only)
    # =========================================================================

    def _check_tethering(self):
        """Basic checks for USB tethering — no AT commands available."""
        self._state["model"] = "USB Tethering"
        self._state["manufacturer"] = "Smartphone"
        self._state["network_type"] = "tethering"
        self._state["sim_status"] = "n/a"

    # =========================================================================
    # Network helpers
    # =========================================================================

    def _detect_interface(self) -> str:
        """Find the LTE network interface."""
        if self._interface != "auto":
            return self._interface

        # E3372h: look for HiLink gateway
        if self._mode == "e3372h":
            try:
                result = subprocess.run(
                    ["ip", "route"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.split("\n"):
                    if "192.168.8.1" in line:
                        m = re.search(r'dev\s+(\S+)', line)
                        if m:
                            return m.group(1)
            except Exception:
                pass

        # SIM7600 RNDIS / tethering: look for usb0
        try:
            result = subprocess.run(
                ["ip", "link", "show"], capture_output=True, text=True, timeout=5)
            for iface in ["usb0", "usb1", "eth1", "wwan0"]:
                if iface in result.stdout and ("state UP" in result.stdout
                        or "state UNKNOWN" in result.stdout):
                    return iface
        except Exception:
            pass

        return ""

    def _get_ip(self, iface: str) -> str:
        """Get the IP address of an interface."""
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", iface],
                capture_output=True, text=True, timeout=5)
            m = re.search(r'inet\s+([\d.]+)', result.stdout)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    def _ping_test(self, iface: str) -> bool:
        """Test internet connectivity via the LTE interface."""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "5", "-I", iface, "8.8.8.8"],
                capture_output=True, timeout=10)
            return result.returncode == 0
        except Exception:
            return False

    # =========================================================================
    # MQTT / HA integration
    # =========================================================================

    def _publish_ha_discovery(self):
        """Publish MQTT discovery configs for all LTE sensors."""
        if not self._client:
            return

        device_name = self._mqtt_cfg.get("device_name", "reef_battery")
        base = self._mqtt_cfg.get("base_topic", "homeassistant")

        device_info = build_device_info(device_name)

        # Sensor definitions: (suffix, name, icon, unit, device_class, entity_category)
        sensors = [
            ("lte_signal", "LTE Signal", "mdi:signal-4g", "dBm",
             "signal_strength", None),
            ("lte_signal_quality", "LTE Signal Quality", "mdi:signal", None,
             None, "diagnostic"),
            ("lte_operator", "LTE Operator", "mdi:antenna", None,
             None, "diagnostic"),
            ("lte_network_type", "LTE Network Type", "mdi:broadcast", None,
             None, "diagnostic"),
            ("lte_sim_status", "LTE SIM Status", "mdi:sim", None,
             None, "diagnostic"),
            ("lte_connected", "LTE Connected", "mdi:check-network", None,
             None, None),
            ("lte_ip", "LTE IP Address", "mdi:ip-network", None,
             None, "diagnostic"),
            ("lte_model", "LTE Modem Model", "mdi:cellphone-wireless", None,
             None, "diagnostic"),
            ("lte_manufacturer", "LTE Manufacturer", "mdi:factory", None,
             None, "diagnostic"),
            ("lte_firmware", "LTE Firmware", "mdi:chip", None,
             None, "diagnostic"),
            ("lte_imei", "LTE IMEI", "mdi:barcode", None,
             None, "diagnostic"),
        ]

        for suffix, name, icon, unit, dev_class, entity_cat in sensors:
            uid = f"{device_name}_{suffix}"
            discovery = {
                "name": name,
                "unique_id": uid,
                "state_topic": f"{base}/sensor/{device_name}/lte/state",
                "value_template": f"{{{{ value_json.{suffix.replace('lte_', '')} }}}}",
                "icon": icon,
                "device": device_info,
            }
            if unit:
                discovery["unit_of_measurement"] = unit
            if dev_class:
                discovery["device_class"] = dev_class
            if entity_cat:
                discovery["entity_category"] = entity_cat

            topic = f"{base}/sensor/{uid}/config"
            self._client.publish(topic, json.dumps(discovery), retain=True)

        print(f"[LTE-MON] Published {len(sensors)} HA discovery configs")

    def _publish_state(self):
        """Publish current LTE state to MQTT."""
        if not self._client:
            return

        device_name = self._mqtt_cfg.get("device_name", "reef_battery")
        base = self._mqtt_cfg.get("base_topic", "homeassistant")

        payload = {
            "signal": self._state.get("signal_strength", 0),
            "signal_quality": self._state.get("signal_quality", 0),
            "operator": self._state.get("operator", ""),
            "network_type": self._state.get("network_type", ""),
            "sim_status": self._state.get("sim_status", "unknown"),
            "connected": "ON" if self._state.get("connected") else "OFF",
            "ip": self._state.get("ip_address", ""),
            "model": self._state.get("model", ""),
            "manufacturer": self._state.get("manufacturer", ""),
            "firmware": self._state.get("firmware", ""),
            "imei": self._state.get("imei", ""),
        }

        topic = f"{base}/sensor/{device_name}/lte/state"
        self._client.publish(topic, json.dumps(payload), retain=True)


# =============================================================================
# Factory
# =============================================================================

def create_lte_monitor(cfg: dict, mqtt_client) -> LteMonitor:
    """Create the LTE monitor from config."""
    return LteMonitor(cfg, mqtt_client)
