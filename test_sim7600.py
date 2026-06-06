#!/usr/bin/env python3
"""
reefbeat⚡Backup — SIM7600G-H 4G Module Test CLI

Tests communication with a Waveshare SIM7600G-H 4G HAT connected
to the Raspberry Pi via USB. Runs through a series of checks:

  1. Detect serial ports (/dev/ttyUSB*)
  2. AT command handshake
  3. SIM card status + PIN handling
  4. Signal quality (RSSI)
  5. Network registration
  6. Operator info
  7. NDIS dial-up (create wwan0/usb0 interface)
  8. Internet connectivity (ping)
  9. Optional: send ntfy test notification via 4G

Usage:
  python3 test_sim7600.py                     # Full test
  python3 test_sim7600.py --port /dev/ttyUSB2 # Specify AT port
  python3 test_sim7600.py --ntfy TOPIC         # Also send ntfy test
  python3 test_sim7600.py --pin 1234           # Provide SIM PIN
  python3 test_sim7600.py --apn free           # Set APN manually
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import time

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False


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
# AT command interface
# =============================================================================

class SIM7600:
    """Simple AT command interface for SIM7600G-H."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 3):
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None

    def open(self) -> bool:
        """Open the serial port."""
        try:
            self._ser = serial.Serial(
                self.port_name,
                self.baudrate,
                timeout=self.timeout
            )
            # Flush any pending data
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            return True
        except serial.SerialException as e:
            fail(f"Cannot open {self.port_name}: {e}")
            return False

    def close(self):
        """Close the serial port."""
        if self._ser and self._ser.is_open:
            self._ser.close()

    def send(self, cmd: str, wait: float = 1.0, expect: str = "OK") -> str:
        """Send an AT command and return the response."""
        if not self._ser or not self._ser.is_open:
            return ""

        # Clear buffer
        self._ser.reset_input_buffer()

        # Send command
        full_cmd = cmd + "\r\n"
        self._ser.write(full_cmd.encode())

        # Wait for response
        time.sleep(wait)

        # Read all available data
        response = ""
        while self._ser.in_waiting > 0:
            chunk = self._ser.read(self._ser.in_waiting)
            response += chunk.decode("utf-8", errors="replace")
            time.sleep(0.1)

        return response.strip()

    def send_check(self, cmd: str, wait: float = 1.0) -> tuple:
        """Send AT command, return (success, response)."""
        resp = self.send(cmd, wait)
        success = "OK" in resp or "CONNECT" in resp
        return success, resp


# =============================================================================
# Test steps
# =============================================================================

def detect_ports() -> list:
    """Find SIM7600 serial ports."""
    ports = sorted(glob.glob("/dev/ttyUSB*"))
    if not ports:
        # Also check for /dev/ttyACM* (some variants)
        ports = sorted(glob.glob("/dev/ttyACM*"))
    return ports


def find_at_port(ports: list) -> str:
    """Find which port responds to AT commands.
    
    SIM7600 typically creates 4 ports:
      ttyUSB0 = diag
      ttyUSB1 = NMEA (GPS)
      ttyUSB2 = AT commands
      ttyUSB3 = modem/PPP
    """
    # Try ttyUSB2 first (most common for AT)
    preferred = [p for p in ports if p.endswith("USB2")]
    candidates = preferred + [p for p in ports if p not in preferred]

    for port in candidates:
        try:
            ser = serial.Serial(port, 115200, timeout=2)
            ser.reset_input_buffer()
            ser.write(b"AT\r\n")
            time.sleep(1)
            resp = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            ser.close()
            if "OK" in resp:
                return port
        except Exception:
            continue
    return ""


def test_sim_status(modem: SIM7600, pin: str = None) -> bool:
    """Check SIM card status, enter PIN if needed."""
    success, resp = modem.send_check("AT+CPIN?", wait=2)

    if "READY" in resp:
        ok("SIM card ready (no PIN required)")
        return True

    if "SIM PIN" in resp:
        warn("SIM card requires PIN")
        if pin:
            info(f"Entering PIN...")
            success, resp = modem.send_check(f'AT+CPIN="{pin}"', wait=3)
            if "OK" in resp:
                ok("PIN accepted")
                time.sleep(5)  # Wait for network registration
                return True
            else:
                fail(f"PIN rejected: {resp}")
                return False
        else:
            fail("PIN required but not provided. Use --pin XXXX")
            return False

    if "SIM PUK" in resp:
        fail("SIM is PUK-locked! Use a phone to unlock it.")
        return False

    if "NOT INSERTED" in resp or "NOT READY" in resp:
        fail("No SIM card detected")
        return False

    warn(f"Unknown SIM state: {resp}")
    return False


def test_signal(modem: SIM7600) -> int:
    """Check signal quality. Returns RSSI value (0-31, 99=unknown)."""
    success, resp = modem.send_check("AT+CSQ")

    match = re.search(r'\+CSQ:\s*(\d+),', resp)
    if match:
        rssi = int(match.group(1))
        if rssi == 99:
            warn("Signal: unknown/no signal")
            return 0
        # Convert RSSI to dBm: dBm = -113 + 2*rssi
        dbm = -113 + 2 * rssi
        bars = (
            "▁" if rssi <= 5 else
            "▁▃" if rssi <= 10 else
            "▁▃▅" if rssi <= 15 else
            "▁▃▅▇" if rssi <= 20 else
            "▁▃▅▇█"
        )
        ok(f"Signal: RSSI {rssi}/31 ({dbm} dBm) {bars}")
        return rssi
    else:
        warn(f"Cannot parse signal: {resp}")
        return 0


def test_registration(modem: SIM7600) -> bool:
    """Check network registration status."""
    success, resp = modem.send_check("AT+CREG?")

    match = re.search(r'\+CREG:\s*\d+,(\d+)', resp)
    if match:
        status = int(match.group(1))
        status_map = {
            0: "Not registered",
            1: "Registered (home network)",
            2: "Searching...",
            3: "Registration denied",
            4: "Unknown",
            5: "Registered (roaming)",
        }
        label = status_map.get(status, f"Unknown ({status})")
        if status in (1, 5):
            ok(f"Network: {label}")
            return True
        else:
            warn(f"Network: {label}")
            return False
    else:
        warn(f"Cannot parse registration: {resp}")
        return False


def test_operator(modem: SIM7600) -> str:
    """Get current operator name."""
    success, resp = modem.send_check("AT+COPS?")

    match = re.search(r'\+COPS:\s*\d+,\d+,"(.+?)"', resp)
    if match:
        operator = match.group(1)
        ok(f"Operator: {operator}")
        return operator
    else:
        info(f"Operator: not available yet")
        return ""


def test_ndis_dialup(modem: SIM7600, apn: str = None) -> bool:
    """Activate NDIS mode to create a network interface.
    
    NDIS (Network Driver Interface Specification) mode makes the
    SIM7600 appear as an Ethernet adapter (wwan0 or usb0) rather
    than requiring PPP dial-up.
    """
    # Set APN if provided
    if apn:
        info(f"Setting APN: {apn}")
        modem.send(f'AT+CGDCONT=1,"IP","{apn}"', wait=2)

    # Check if already connected
    success, resp = modem.send_check("AT$QCRMCALL?", wait=2)
    if "V4" in resp:
        ok("NDIS already connected")
        return True

    # Start NDIS dial-up
    info("Starting NDIS dial-up...")
    success, resp = modem.send_check("AT$QCRMCALL=1,1", wait=5)

    if "OK" in resp or "CONNECT" in resp:
        ok("NDIS dial-up successful")
        time.sleep(3)  # Wait for interface to come up
        return True
    else:
        warn(f"NDIS dial-up response: {resp}")
        # Try alternate method
        info("Trying alternate dial-up (AT+NETOPEN)...")
        success, resp = modem.send_check("AT+NETOPEN", wait=5)
        if "OK" in resp or "already" in resp.lower():
            ok("Network opened via AT+NETOPEN")
            return True
        warn(f"Alternate dial-up failed: {resp}")
        return False


def find_wwan_interface() -> str:
    """Find the network interface created by the SIM7600."""
    try:
        result = subprocess.run(
            ["ip", "link", "show"], capture_output=True, text=True, timeout=5
        )
        for iface in ["wwan0", "usb0", "eth1", "wwan1"]:
            if iface in result.stdout:
                # Check if it has an IP
                addr_result = subprocess.run(
                    ["ip", "addr", "show", iface],
                    capture_output=True, text=True, timeout=5
                )
                if "inet " in addr_result.stdout:
                    ip_match = re.search(r'inet\s+([\d.]+)', addr_result.stdout)
                    ip_addr = ip_match.group(1) if ip_match else "?"
                    ok(f"Network interface: {iface} ({ip_addr})")
                    return iface
                elif "state UP" in result.stdout or "state UNKNOWN" in result.stdout:
                    warn(f"Interface {iface} found but no IP yet")
                    # Try DHCP
                    info(f"Requesting IP via DHCP on {iface}...")
                    subprocess.run(
                        ["sudo", "dhclient", iface],
                        capture_output=True, timeout=15
                    )
                    time.sleep(3)
                    addr_result = subprocess.run(
                        ["ip", "addr", "show", iface],
                        capture_output=True, text=True, timeout=5
                    )
                    if "inet " in addr_result.stdout:
                        ip_match = re.search(r'inet\s+([\d.]+)', addr_result.stdout)
                        ip_addr = ip_match.group(1) if ip_match else "?"
                        ok(f"Network interface: {iface} ({ip_addr})")
                        return iface
    except Exception as e:
        warn(f"Interface detection error: {e}")
    return ""


def test_connectivity(iface: str) -> bool:
    """Test internet connectivity via the 4G interface."""
    info(f"Testing internet connectivity via {iface}...")
    try:
        result = subprocess.run(
            ["ping", "-c", "3", "-W", "5", "-I", iface, "8.8.8.8"],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0:
            # Extract latency
            match = re.search(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)', result.stdout)
            avg_ms = match.group(1) if match else "?"
            ok(f"Internet OK via {iface} (avg latency: {avg_ms} ms)")
            return True
        else:
            fail(f"Ping failed via {iface}")
            return False
    except Exception as e:
        fail(f"Connectivity test error: {e}")
        return False


def test_ntfy(iface: str, topic: str, server: str = "https://ntfy.sh") -> bool:
    """Send a test notification via ntfy through the 4G interface."""
    info(f"Sending ntfy test via {iface}...")
    try:
        result = subprocess.run([
            "curl", "-s", "--max-time", "15",
            "--interface", iface,
            "-H", "Title: reefbeat Backup -- Test SIM7600",
            "-H", "Priority: default",
            "-H", "Tags: satellite,zap",
            "-d", "Test notification via SIM7600G-H 4G -- OK!",
            f"{server}/{topic}"
        ], capture_output=True, timeout=20)

        if result.returncode == 0:
            ok("Notification sent via SIM7600!")
            return True
        else:
            fail(f"curl failed: {result.stderr.decode()[:100]}")
            return False
    except Exception as e:
        fail(f"ntfy error: {e}")
        return False


def get_module_info(modem: SIM7600):
    """Print module identification info."""
    # Manufacturer
    _, resp = modem.send_check("AT+CGMI")
    mfr = resp.replace("AT+CGMI", "").replace("OK", "").strip()

    # Model
    _, resp = modem.send_check("AT+CGMM")
    model = resp.replace("AT+CGMM", "").replace("OK", "").strip()

    # Firmware
    _, resp = modem.send_check("AT+CGMR")
    fw = resp.replace("AT+CGMR", "").replace("OK", "").strip()
    # Clean up multi-line response
    fw = fw.split("\n")[0].strip() if "\n" in fw else fw

    # IMEI
    _, resp = modem.send_check("AT+CGSN")
    imei = resp.replace("AT+CGSN", "").replace("OK", "").strip()
    imei = imei.split("\n")[0].strip() if "\n" in imei else imei

    info(f"Manufacturer: {mfr}")
    info(f"Model: {model}")
    info(f"Firmware: {fw}")
    info(f"IMEI: {imei}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="reefbeat Backup -- SIM7600G-H 4G module test"
    )
    parser.add_argument("--port", "-p", default=None,
                        help="Serial port (default: auto-detect)")
    parser.add_argument("--pin", default=None,
                        help="SIM card PIN code")
    parser.add_argument("--apn", default=None,
                        help="APN name (usually auto-detected)")
    parser.add_argument("--ntfy", default=None, metavar="TOPIC",
                        help="Send a test notification to this ntfy topic")
    parser.add_argument("--ntfy-server", default="https://ntfy.sh",
                        help="ntfy server URL")
    args = parser.parse_args()

    header("SIM7600G-H 4G Module Test")

    # Check pyserial
    if not HAS_SERIAL:
        fail("pyserial not installed")
        info("Install with: pip install pyserial --break-system-packages")
        sys.exit(1)

    # Step 1: Detect ports
    section("1. Serial port detection")
    ports = detect_ports()
    if not ports:
        fail("No /dev/ttyUSB* ports found")
        info("Check that the SIM7600G-H is connected via USB")
        info("  - PWR LED should be ON")
        info("  - NET LED should blink once per second")
        sys.exit(1)

    ok(f"Found {len(ports)} serial port(s): {', '.join(ports)}")

    # Find AT port
    at_port = args.port
    if not at_port:
        info("Auto-detecting AT command port...")
        at_port = find_at_port(ports)
        if not at_port:
            fail("No port responds to AT commands")
            info("Try specifying manually: --port /dev/ttyUSB2")
            sys.exit(1)
    ok(f"AT port: {at_port}")

    # Open modem
    modem = SIM7600(at_port)
    if not modem.open():
        sys.exit(1)

    try:
        # Step 2: AT handshake
        section("2. Module identification")
        success, resp = modem.send_check("AT")
        if not success:
            fail("Module not responding to AT command")
            sys.exit(1)
        ok("AT handshake OK")
        get_module_info(modem)

        # Step 3: SIM status
        section("3. SIM card status")
        sim_ok = test_sim_status(modem, pin=args.pin)
        if not sim_ok:
            warn("SIM not ready — network tests will be skipped")

        # Step 4: Signal quality
        section("4. Signal quality")
        rssi = test_signal(modem)

        # Step 5: Network registration
        section("5. Network registration")
        registered = test_registration(modem) if sim_ok else False

        # Step 6: Operator
        if registered:
            section("6. Operator info")
            test_operator(modem)

        # Step 7: NDIS dial-up
        section("7. NDIS dial-up")
        if registered:
            ndis_ok = test_ndis_dialup(modem, apn=args.apn)
        else:
            warn("Skipping — not registered on network")
            ndis_ok = False

        # Step 8: Network interface
        section("8. Network interface")
        iface = ""
        if ndis_ok:
            iface = find_wwan_interface()
            if not iface:
                warn("No network interface found after NDIS dial-up")
                info("The module may need a few more seconds, or try:")
                info("  sudo dhclient wwan0")
        else:
            warn("Skipping — NDIS not active")

        # Step 9: Internet connectivity
        section("9. Internet connectivity")
        internet_ok = False
        if iface:
            internet_ok = test_connectivity(iface)
        else:
            warn("Skipping — no interface available")

        # Step 10: ntfy test
        if args.ntfy and internet_ok:
            section("10. ntfy notification test")
            test_ntfy(iface, args.ntfy, args.ntfy_server)
        elif args.ntfy and not internet_ok:
            section("10. ntfy notification test")
            warn("Skipping — no internet connectivity")

        # Summary
        header("Test Summary")
        results = [
            ("Serial port", True),
            ("AT handshake", True),
            ("SIM card", sim_ok),
            ("Signal", rssi > 0),
            ("Registration", registered),
            ("NDIS dial-up", ndis_ok),
            ("Network interface", bool(iface)),
            ("Internet", internet_ok),
        ]
        for name, passed in results:
            icon = f"{C.OK}✓{C.END}" if passed else f"{C.FAIL}✗{C.END}"
            print(f"  {icon} {name}")

        passed = sum(1 for _, p in results if p)
        total = len(results)
        print()
        if passed == total:
            ok(f"{C.BOLD}All {total} tests passed!{C.END}")
        else:
            warn(f"{C.BOLD}{passed}/{total} tests passed{C.END}")

        if internet_ok:
            print()
            info(f"The SIM7600G-H is ready. Interface: {iface}")
            info("Add it to configure.py as a 4G failover option.")

    finally:
        modem.close()


if __name__ == "__main__":
    main()
