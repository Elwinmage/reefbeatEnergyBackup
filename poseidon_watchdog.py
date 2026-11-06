#!/usr/bin/env python3
"""Watchdog of the Poseidon relay of the Home Assistant redsea integration.

Poseidon mode points the ReefBeat devices at an MQTT relay running inside
Home Assistant. When that relay dies, the devices lose the Red Sea cloud and
the app stops working: this module, running on another machine, watches the
relay and switches the devices back to the Red Sea broker.

The module has no dependency on Home Assistant nor on the rest of this
project. It runs inside the energy backup service (see main.py), or alone:

    python3 poseidon_watchdog.py --config config.json status
    python3 poseidon_watchdog.py --config config.json run

Protocol (custom_components/redsea/poseidon/watchdog.py on the other side):
connect to the relay port, the very path the devices use, with the reserved
login "poseidon-watchdog", then

    poseidon/is_alive           {"id", "ts", "minutes"}       (watchdog -> relay)
    poseidon/is_alive/response  {"id", "ts", "ok", "devices"} (relay -> watchdog)

Both payloads use the AES-128-CBC format of the devices, with the 16-character
watchdog key of the Poseidon options. The key never goes on the wire: a request
that does not decrypt gets no answer. "ok" is true when at least one device
exchanged frames with Red Sea in both directions within "minutes", so a single
device under maintenance never switches the others. "devices" lists every
device in Poseidon mode with its keys, because the firmware only accepts the
url, the api_key and the api_secret together.

Algorithm:

    every check_interval_min:
        is_alive(minutes); an answer refreshes the cache of the devices
        ok, or nothing in Poseidon mode: done
        wait retry_delay_min, is_alive(minutes) again; ok: done
        Red Sea broker unreachable from here: done (Internet is down, falling
            back would only wear the flash of the devices)
        every cached device back to normal mode, then notify

The fallback is global, and the watchdog only ever goes towards normal mode:
entering Poseidon mode stays a decision of the user, in Home Assistant.

A device on a hotspot subnet cannot reach the relay: before the energy backup
activates its hotspot, the cached devices are switched back without any check
(before_hotspot), and those still out of reach are retried at the addresses
of the hotspot clients (hotspot_clients).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

WATCHDOG_USERNAME = "poseidon-watchdog"
TOPIC_REQUEST = "poseidon/is_alive"
TOPIC_RESPONSE = "poseidon/is_alive/response"
KEY_LENGTH = 16

NORMAL_HOST = "mqtt.reef-beat.com"
NORMAL_PORT = 1883
NORMAL_URL = f"{NORMAL_HOST}:{NORMAL_PORT}"

DEFAULT_PORT = 1884
DEFAULT_CHECK_INTERVAL_MIN = 5.0
DEFAULT_MINUTES = 10
DEFAULT_RETRY_DELAY_MIN = 10.0
DEFAULT_CACHE_FILE = "/var/lib/reefbeat-energy-backup/poseidon_watchdog.json"

MQTT_TIMEOUT_S = 15.0
HTTP_TIMEOUT_S = 8.0
# Hotspot clients are probed from the failover loop: keep it short
PROBE_TIMEOUT_S = 2.0
BROKER_TIMEOUT_S = 5.0
# Time left to a device to reconnect before its /cloud is read again
SETTLE_S = 2.0

CONNACK = 0x20
PUBLISH = 0x30
SUBACK = 0x90

# Results of a check, for the logs and the tests
STATE_OK = "ok"
STATE_IDLE = "idle"
STATE_RECOVERED = "recovered"
STATE_INTERNET_DOWN = "internet_down"
STATE_FALLBACK = "fallback"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"


class WatchdogError(Exception):
    """The relay did not give a usable answer."""


# =============================================================================
# Payloads
# =============================================================================


def encrypt_payload(data: Any, key: str) -> str:
    """base64(ciphertext).base64(iv), the format of the devices."""
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return (
        base64.b64encode(ciphertext).decode("ascii")
        + "."
        + base64.b64encode(iv).decode("ascii")
    )


def decrypt_payload(encrypted: str, key: str) -> Any:
    """The JSON held by an encrypted payload."""
    parts = encrypted.strip().split(".")
    if len(parts) != 2:
        raise ValueError("expected 'ciphertext.iv'")
    ciphertext = base64.b64decode(parts[0])
    iv = base64.b64decode(parts[1])
    decryptor = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads((unpadder.update(padded) + unpadder.finalize()).decode("utf-8"))


# =============================================================================
# MQTT: the few packets the session needs
# =============================================================================


def _utf8(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def _packet(first: int, body: bytes) -> bytes:
    """A packet: its first byte, its remaining length, its body."""
    length = len(body)
    encoded = bytearray()
    while True:
        length, digit = divmod(length, 128)
        encoded.append(digit | (0x80 if length else 0))
        if not length:
            return bytes([first]) + bytes(encoded) + body


def _connect() -> bytes:
    # Protocol 3.1.1, clean session, username only, keepalive 60 s
    variable = _utf8("MQTT") + bytes([4, 0x82]) + struct.pack("!H", 60)
    return _packet(0x10, variable + _utf8(WATCHDOG_USERNAME) + _utf8(WATCHDOG_USERNAME))


def _subscribe(topic: str) -> bytes:
    return _packet(0x82, struct.pack("!H", 1) + _utf8(topic) + bytes([0]))


def _publish(topic: str, payload: bytes) -> bytes:
    return _packet(PUBLISH, _utf8(topic) + payload)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise WatchdogError("connection closed by the relay")
        data.extend(chunk)
    return bytes(data)


def _read_packet(sock: socket.socket) -> tuple:
    """(packet type, body) of the next packet."""
    first = _read_exact(sock, 1)[0]
    length, shift = 0, 0
    while True:
        digit = _read_exact(sock, 1)[0]
        length += (digit & 0x7F) << shift
        shift += 7
        if not digit & 0x80:
            break
        if shift > 21:
            raise WatchdogError("malformed packet")
    return first & 0xF0, _read_exact(sock, length)


def is_alive(
    host: str,
    port: int,
    key: str,
    minutes: int = DEFAULT_MINUTES,
    timeout: float = MQTT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Ask the relay whether it is alive.

    @return: its decrypted answer
    @raise WatchdogError: no usable answer (down, wrong key, old answer)
    """
    request_id = uuid.uuid4().hex
    request = {
        "id": request_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "minutes": int(minutes),
    }
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(_connect())
            packet, body = _read_packet(sock)
            if packet != CONNACK or len(body) < 2 or body[1] != 0:
                raise WatchdogError("connection refused by the relay")
            sock.sendall(_subscribe(TOPIC_RESPONSE))
            sock.sendall(
                _publish(TOPIC_REQUEST, encrypt_payload(request, key).encode("ascii"))
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                packet, body = _read_packet(sock)
                if packet != PUBLISH:
                    continue
                size = struct.unpack("!H", body[:2])[0]
                if body[2 : 2 + size].decode("utf-8") != TOPIC_RESPONSE:
                    continue
                answer = decrypt_payload(body[2 + size :].decode("ascii"), key)
                if not isinstance(answer, dict) or answer.get("id") != request_id:
                    raise WatchdogError("answer to another request")
                return answer
    except (OSError, ValueError, struct.error) as err:
        raise WatchdogError(str(err) or type(err).__name__) from err
    raise WatchdogError("no answer")


# =============================================================================
# Devices
# =============================================================================


def broker_of(url: Any) -> str:
    """A broker address, normalised ("" when unknown)."""
    return str(url or "").strip().lower().split("://", 1)[-1].rstrip("/")


def _http(
    ip: str,
    method: str,
    endpoint: str,
    payload: Any = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> Any:
    """One local HTTP call; the decoded answer (None if not JSON)."""
    data = None if method == "GET" else b""
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://{ip}{endpoint}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


def identify(ip: str, timeout: float = HTTP_TIMEOUT_S) -> str:
    """Hardware id of the ReefBeat device answering at ip ("" if none)."""
    try:
        info = _http(ip, "GET", "/device-info", timeout=timeout)
    except (OSError, urllib.error.URLError, ValueError):
        return ""
    return str(info.get("hwid") or "").strip().lower() if isinstance(info, dict) else ""


def get_cloud(ip: str) -> Optional[Dict[str, Any]]:
    try:
        cloud = _http(ip, "GET", "/cloud")
    except (OSError, urllib.error.URLError, ValueError):
        return None
    return cloud if isinstance(cloud, dict) else None


def _post(ip: str, endpoint: str, payload: Any = None) -> bool:
    try:
        _http(ip, "POST", endpoint, payload)
    except (OSError, urllib.error.URLError) as err:
        print(f"[POSEIDON] POST {ip}{endpoint}: {err}")
        return False
    return True


def set_cloud(ip: str, url: str, api_key: str, api_secret: str) -> bool:
    """The sequence of the official app; the firmware needs the three fields."""
    disabled = _post(ip, "/cloud/disable")
    written = _post(
        ip, "/cloud", {"url": url, "api_key": api_key, "api_secret": api_secret}
    )
    # Enabled again even after a failed write: never leave a device cut off
    enabled = _post(ip, "/cloud/enable")
    return disabled and written and enabled


def switch_back(hwid: str, device: Dict[str, Any], ip: str) -> bool:
    """Put one device back on its normal broker; whether it is there now.

    Nothing is written before the device at that address proves to be the
    right one: keys written into another device would cut that one off. A
    device already on its normal broker gets no write (the flash wears).
    """
    if identify(ip) != hwid.lower():
        print(f"[POSEIDON] {hwid}: not found at {ip}")
        return False
    target = broker_of(device.get("mqtt_url")) or NORMAL_URL
    cloud = get_cloud(ip)
    if cloud is not None and broker_of(cloud.get("url")) == target:
        return True
    api_key, api_secret = device.get("api_key"), device.get("api_secret")
    if not api_key or not api_secret:
        print(f"[POSEIDON] {hwid}: no keys")
        return False
    if not set_cloud(ip, target, api_key, api_secret):
        return False
    time.sleep(SETTLE_S)
    cloud = get_cloud(ip)
    return cloud is not None and broker_of(cloud.get("url")) == target


def broker_reachable(host: str = NORMAL_HOST, port: int = NORMAL_PORT) -> bool:
    """Whether the Red Sea broker answers from this machine."""
    try:
        with socket.create_connection((host, port), timeout=BROKER_TIMEOUT_S):
            return True
    except OSError:
        return False


# =============================================================================
# Watchdog
# =============================================================================


class PoseidonWatchdog:
    """The watch loop, with the cache of the devices.

    @param cfg: the "poseidon_watchdog" section of the configuration
    @param notify: called with (title, message, critical)
    @param paused: while it returns True nothing is checked (hotspot)
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        notify: Optional[Callable[[str, str, bool], None]] = None,
        paused: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._host = str(cfg.get("host") or "")
        self._port = int(cfg.get("port") or DEFAULT_PORT)
        self._key = str(cfg.get("key") or "")
        self._interval_s = (
            float(cfg.get("check_interval_min", DEFAULT_CHECK_INTERVAL_MIN)) * 60
        )
        self._minutes = int(cfg.get("minutes", DEFAULT_MINUTES))
        self._retry_s = float(cfg.get("retry_delay_min", DEFAULT_RETRY_DELAY_MIN)) * 60
        self._cache_path = Path(cfg.get("cache_file") or DEFAULT_CACHE_FILE)
        self._notify_hook = notify
        self._paused = paused or (lambda: False)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # An incident is told once, and its end too
        self._failing = False
        self._internet_down_told = False
        # Left to switch back while the hotspot is up
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._probed: Dict[str, str] = {}
        self._devices = self._load_cache()

    @property
    def devices(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._devices)

    def problem(self) -> str:
        """What keeps the watchdog from running ("" when nothing does)."""
        if not HAS_CRYPTOGRAPHY:
            return "the cryptography package is missing (pip install cryptography)"
        if not self._host:
            return "no relay host"
        if len(self._key.encode("utf-8")) != KEY_LENGTH:
            return f"the watchdog key must be {KEY_LENGTH} characters"
        return ""

    # -------------------------------------------------------------------------
    # Cache: it holds the keys of the devices
    # -------------------------------------------------------------------------

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cache(self) -> None:
        tmp = self._cache_path.with_suffix(".tmp")
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._devices, f)
            os.replace(tmp, self._cache_path)
        except OSError as err:
            print(f"[POSEIDON] Cannot write {self._cache_path}: {err}")

    def _set_devices(self, devices: Any) -> None:
        if not isinstance(devices, dict):
            return
        with self._lock:
            if devices != self._devices:
                self._devices = devices
                self._save_cache()

    # -------------------------------------------------------------------------
    # Checks
    # -------------------------------------------------------------------------

    def _notify(self, title: str, message: str, critical: bool = False) -> None:
        print(f"[POSEIDON] {title}: {message}")
        if self._notify_hook:
            try:
                self._notify_hook(title, message, critical)
            except Exception as err:  # a notification never stops the watch
                print(f"[POSEIDON] Notification failed: {err}")

    def _ask(self) -> Optional[Dict[str, Any]]:
        """The answer of the relay, its devices cached; None without one."""
        try:
            answer = is_alive(self._host, self._port, self._key, self._minutes)
        except WatchdogError as err:
            print(f"[POSEIDON] is_alive failed: {err}")
            return None
        self._set_devices(answer.get("devices"))
        return answer

    @staticmethod
    def _healthy(answer: Optional[Dict[str, Any]]) -> bool:
        """Alive, or answering with nothing to watch."""
        return answer is not None and bool(
            answer.get("ok") or not answer.get("devices")
        )

    def _back_to_normal(self, state: str) -> str:
        if self._failing:
            self._notify("Poseidon relay back", "The relay answers again.")
        self._failing = False
        self._internet_down_told = False
        return state

    def check(self) -> str:
        """One round of the algorithm of the module."""
        if self._paused():
            return STATE_PAUSED
        answer = self._ask()
        if self._healthy(answer):
            idle = not answer.get("ok")
            return self._back_to_normal(STATE_IDLE if idle else STATE_OK)

        with self._lock:
            known = len(self._devices)
        if not self._failing:
            self._failing = True
            self._notify(
                "Poseidon relay not answering",
                f"Checking again in {self._retry_s / 60:.0f} min; "
                f"{known} device(s) would go back to normal mode.",
            )
        if self._stop.wait(timeout=self._retry_s):
            return STATE_STOPPED
        if self._paused():
            return STATE_PAUSED
        answer = self._ask()
        if self._healthy(answer):
            return self._back_to_normal(STATE_RECOVERED)

        if not broker_reachable():
            if not self._internet_down_told:
                self._internet_down_told = True
                self._notify(
                    "Poseidon relay down, Internet too",
                    "The Red Sea broker is out of reach: devices left as they are.",
                )
            return STATE_INTERNET_DOWN
        self._internet_down_told = False

        switched, failed = self._switch_all()
        if not switched and not failed:
            self._notify(
                "Poseidon relay down",
                "No device known: check the host, the port and the watchdog key.",
                critical=True,
            )
        else:
            self._report("Poseidon relay down", switched, failed)
        return STATE_FALLBACK

    def _switch_all(self) -> tuple:
        """Every cached device back to normal mode: (done, failed) names."""
        with self._lock:
            devices = dict(self._devices)
        done: Dict[str, str] = {}
        failed: Dict[str, str] = {}
        for hwid, device in devices.items():
            ip = str(device.get("ip") or "")
            name = str(device.get("name") or hwid)
            if ip and switch_back(hwid, device, ip):
                done[hwid] = name
            else:
                failed[hwid] = name
        with self._lock:
            for hwid in done:
                self._devices.pop(hwid, None)
            if done:
                self._save_cache()
        return done, failed

    def _report(
        self, title: str, switched: Dict[str, str], failed: Dict[str, str]
    ) -> None:
        lines = []
        if switched:
            lines.append("Back to normal mode: " + ", ".join(switched.values()))
        if failed:
            lines.append("Not reached: " + ", ".join(failed.values()))
        lines.append("Poseidon mode is only entered again from Home Assistant.")
        self._notify(title, "\n".join(lines), critical=True)

    # -------------------------------------------------------------------------
    # Hotspot of the energy backup
    # -------------------------------------------------------------------------

    def before_hotspot(self) -> None:
        """Devices on the hotspot cannot reach the relay: switch them first."""
        with self._lock:
            has_devices = bool(self._devices)
        if not has_devices:
            return
        print("[POSEIDON] Hotspot coming: devices back to normal mode")
        switched, failed = self._switch_all()
        with self._lock:
            self._pending = {h: self._devices[h] for h in failed if h in self._devices}
            self._probed = {}
        if switched or failed:
            self._report("Poseidon: hotspot failover", switched, failed)

    def hotspot_clients(self, ips: Iterable[str]) -> None:
        """Retry the devices left out, once they joined the hotspot."""
        with self._lock:
            if not self._pending:
                return
            new = [ip for ip in ips if ip not in self._probed]
        for ip in new:
            hwid = identify(ip, timeout=PROBE_TIMEOUT_S)
            with self._lock:
                self._probed[ip] = hwid
                device = self._pending.get(hwid)
            if device is None or not switch_back(hwid, device, ip):
                continue
            with self._lock:
                self._pending.pop(hwid, None)
                self._devices.pop(hwid, None)
                self._save_cache()
            self._report(
                "Poseidon: hotspot failover", {hwid: str(device.get("name"))}, {}
            )

    # -------------------------------------------------------------------------
    # Thread
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """Check until stopped."""
        print(
            f"[POSEIDON] Watching {self._host}:{self._port} "
            f"every {self._interval_s / 60:.0f} min"
        )
        while not self._stop.is_set():
            try:
                state = self.check()
            except Exception as err:  # the watch outlives any single round
                print(f"[POSEIDON] Check failed: {err}")
                state = "error"
            if state not in (STATE_OK, STATE_IDLE):
                print(f"[POSEIDON] State: {state}")
            self._stop.wait(timeout=self._interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.run, name="poseidon-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def create_poseidon_watchdog(
    cfg: Dict[str, Any],
    notify: Optional[Callable[[str, str, bool], None]] = None,
    paused: Optional[Callable[[], bool]] = None,
) -> Optional[PoseidonWatchdog]:
    """The watchdog of a configuration, None when it is off or unusable."""
    section = cfg.get("poseidon_watchdog", {})
    if not section.get("enabled"):
        return None
    watchdog = PoseidonWatchdog(section, notify, paused)
    problem = watchdog.problem()
    if problem:
        print(f"[POSEIDON] Disabled: {problem}")
        return None
    return watchdog


# =============================================================================
# CLI
# =============================================================================


def _status(watchdog: PoseidonWatchdog, section: Dict[str, Any]) -> int:
    try:
        answer = is_alive(
            str(section.get("host")),
            int(section.get("port") or DEFAULT_PORT),
            str(section.get("key")),
            int(section.get("minutes", DEFAULT_MINUTES)),
        )
    except WatchdogError as err:
        print(f"No answer: {err}")
        return 1
    print(f"ok: {answer.get('ok')}")
    for hwid, device in (answer.get("devices") or {}).items():
        print(
            f"- {device.get('name') or hwid} [{device.get('ip')}] "
            f"up {device.get('last_up')} down {device.get('last_down')}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog of the Poseidon relay")
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "command",
        choices=["status", "run"],
        help="status: ask the relay once; run: watch it until stopped",
    )
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as f:
        section = json.load(f).get("poseidon_watchdog", {})
    watchdog = PoseidonWatchdog(section)
    problem = watchdog.problem()
    if problem:
        print(f"Error: {problem}")
        return 2
    if args.command == "status":
        return _status(watchdog, section)
    try:
        watchdog.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
