#!/usr/bin/env python3
"""Tests of poseidon_watchdog.py: pytest test_poseidon_watchdog.py"""

import json
import socket
import stat
import struct
import threading
from typing import Any, Dict, List

import pytest

import poseidon_watchdog as pw

KEY = "0123456789abcdef"


def device(ip: str = "10.0.0.2", name: str = "Wave") -> Dict[str, Any]:
    return {
        "name": name,
        "type": "reef-wave",
        "ip": ip,
        "api_key": "api-key",
        "api_secret": "api-secret-16-ch",
        "mqtt_url": "mqtt.reef-beat.com:1883",
    }


class FakeRelay:
    """The server side of the protocol, on a real socket."""

    def __init__(self, answer: Any = None, key: str = KEY) -> None:
        self.answer = answer or {"ok": True, "devices": {}}
        self.key = key
        self.silent = False
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.logins: List[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    self._session(conn)
                except (pw.WatchdogError, OSError, ValueError):
                    pass

    def _session(self, conn: socket.socket) -> None:
        _, body = pw._read_packet(conn)
        self.logins.append(body)
        conn.sendall(bytes([pw.CONNACK, 2, 0, 0]))
        while True:
            packet, body = pw._read_packet(conn)
            if packet == 0x80:
                conn.sendall(bytes([pw.SUBACK, 3, 0, 1, 0]))
            elif packet == pw.PUBLISH:
                size = struct.unpack("!H", body[:2])[0]
                request = pw.decrypt_payload(body[2 + size :].decode(), self.key)
                if self.silent:
                    return
                response = {**self.answer, "id": self.answer.get("id", request["id"])}
                payload = pw.encrypt_payload(response, self.key).encode()
                conn.sendall(pw._publish(pw.TOPIC_RESPONSE, payload))

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def relay():
    fake = FakeRelay()
    yield fake
    fake.close()


@pytest.fixture
def notes():
    return []


def watchdog(relay: FakeRelay, tmp_path, notes, **cfg: Any) -> pw.PoseidonWatchdog:
    section = {
        "host": "127.0.0.1",
        "port": relay.port,
        "key": KEY,
        "retry_delay_min": 0,
        "cache_file": str(tmp_path / "cache.json"),
        **cfg,
    }
    return pw.PoseidonWatchdog(
        section, notify=lambda title, _msg, _crit: notes.append(title)
    )


def test_payload_round_trip():
    assert pw.decrypt_payload(pw.encrypt_payload({"a": 1}, KEY), KEY) == {"a": 1}


def test_large_packet_length():
    packet = pw._packet(pw.PUBLISH, b"x" * 300)
    assert packet[1:3] == bytes([0xAC, 0x02])


def test_is_alive_logs_in_with_the_reserved_name(relay):
    answer = pw.is_alive("127.0.0.1", relay.port, KEY, timeout=2)
    assert answer["ok"] is True
    assert pw.WATCHDOG_USERNAME.encode() in relay.logins[0]


def test_is_alive_large_answer(relay):
    relay.answer = {
        "ok": True,
        "devices": {f"{i:012x}": device(name="n" * 100) for i in range(40)},
    }
    answer = pw.is_alive("127.0.0.1", relay.port, KEY, timeout=2)
    assert len(answer["devices"]) == 40


def test_is_alive_refuses_a_replayed_answer(relay):
    relay.answer = {"ok": True, "id": "old"}
    with pytest.raises(pw.WatchdogError, match="another request"):
        pw.is_alive("127.0.0.1", relay.port, KEY, timeout=2)


def test_is_alive_wrong_key_gets_no_answer():
    relay = FakeRelay(key="another-16-chars")
    with pytest.raises(pw.WatchdogError):
        pw.is_alive("127.0.0.1", relay.port, KEY, timeout=0.5)
    relay.close()


def test_is_alive_relay_down():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(pw.WatchdogError):
        pw.is_alive("127.0.0.1", port, KEY, timeout=0.5)


def test_healthy_check_caches_the_devices_privately(relay, tmp_path, notes):
    relay.answer = {"ok": True, "devices": {"aa": device()}}
    dog = watchdog(relay, tmp_path, notes)
    assert dog.check() == pw.STATE_OK
    cache = tmp_path / "cache.json"
    assert json.loads(cache.read_text())["aa"]["api_key"] == "api-key"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert notes == []


def test_nothing_in_poseidon_mode_is_not_a_failure(relay, tmp_path, notes):
    relay.answer = {"ok": False, "devices": {}}
    assert watchdog(relay, tmp_path, notes).check() == pw.STATE_IDLE


def test_cache_survives_a_restart(relay, tmp_path, notes):
    relay.answer = {"ok": True, "devices": {"aa": device()}}
    watchdog(relay, tmp_path, notes).check()
    relay.close()
    assert "aa" in watchdog(relay, tmp_path, notes).devices


def failing(monkeypatch, responses: List[Any]) -> None:
    """is_alive answering each of `responses` in turn (None: no answer)."""
    queue = list(responses)

    def fake(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        answer = queue.pop(0)
        if answer is None:
            raise pw.WatchdogError("down")
        return answer

    monkeypatch.setattr(pw, "is_alive", fake)


def test_retry_recovers_without_touching_a_device(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    dog._set_devices({"aa": device()})
    failing(monkeypatch, [None, {"ok": True, "devices": {"aa": device()}}])
    monkeypatch.setattr(pw, "switch_back", lambda *_: pytest.fail("switched"))
    assert dog.check() == pw.STATE_RECOVERED
    assert notes == ["Poseidon relay not answering", "Poseidon relay back"]


def test_internet_down_leaves_the_devices_alone(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    dog._set_devices({"aa": device()})
    failing(monkeypatch, [None, None, None, None])
    monkeypatch.setattr(pw, "broker_reachable", lambda: False)
    monkeypatch.setattr(pw, "switch_back", lambda *_: pytest.fail("switched"))
    assert dog.check() == pw.STATE_INTERNET_DOWN
    assert dog.check() == pw.STATE_INTERNET_DOWN
    # Told once per incident
    assert notes.count("Poseidon relay down, Internet too") == 1


def test_fallback_is_global_and_keeps_the_failed(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    dog._set_devices({"aa": device("10.0.0.2"), "bb": device("10.0.0.3")})
    failing(monkeypatch, [None, None])
    monkeypatch.setattr(pw, "broker_reachable", lambda: True)
    calls = []

    def switch(hwid: str, _device: Any, ip: str) -> bool:
        calls.append((hwid, ip))
        return hwid == "aa"

    monkeypatch.setattr(pw, "switch_back", switch)
    assert dog.check() == pw.STATE_FALLBACK
    assert calls == [("aa", "10.0.0.2"), ("bb", "10.0.0.3")]
    assert list(dog.devices) == ["bb"]
    assert "Poseidon relay down" in notes


def test_relay_up_but_no_traffic_falls_back(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    answer = {"ok": False, "devices": {"aa": device()}}
    failing(monkeypatch, [answer, answer])
    monkeypatch.setattr(pw, "broker_reachable", lambda: True)
    monkeypatch.setattr(pw, "switch_back", lambda *_: True)
    assert dog.check() == pw.STATE_FALLBACK
    assert dog.devices == {}


def test_no_device_known_is_a_configuration_alert(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    failing(monkeypatch, [None, None])
    monkeypatch.setattr(pw, "broker_reachable", lambda: True)
    assert dog.check() == pw.STATE_FALLBACK
    assert "Poseidon relay down" in notes


def test_paused_during_a_hotspot(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    dog._paused = lambda: True
    monkeypatch.setattr(pw, "is_alive", lambda *_: pytest.fail("asked"))
    assert dog.check() == pw.STATE_PAUSED


def test_stop_during_the_retry_wait(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes, retry_delay_min=10)
    failing(monkeypatch, [None])
    dog._stop.set()
    assert dog.check() == pw.STATE_STOPPED


def test_before_hotspot_needs_no_failure(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    dog._set_devices({"aa": device(), "bb": device("10.0.0.3", "Run")})
    monkeypatch.setattr(pw, "switch_back", lambda hwid, *_: hwid == "aa")
    monkeypatch.setattr(pw, "broker_reachable", lambda: pytest.fail("checked"))
    dog.before_hotspot()
    assert list(dog.devices) == ["bb"]
    assert list(dog._pending) == ["bb"]


def test_hotspot_clients_catch_the_late_devices(relay, tmp_path, notes, monkeypatch):
    dog = watchdog(relay, tmp_path, notes)
    dog._set_devices({"bb": device("10.0.0.3", "Run")})
    monkeypatch.setattr(pw, "switch_back", lambda *_: False)
    dog.before_hotspot()
    probes = []

    def identify(ip: str, timeout: float = 0) -> str:
        probes.append(ip)
        return "bb" if ip == "192.168.4.12" else "cc"

    switched = []

    def switch(hwid: str, _device: Any, ip: str) -> bool:
        switched.append((hwid, ip))
        return True

    monkeypatch.setattr(pw, "identify", identify)
    monkeypatch.setattr(pw, "switch_back", switch)
    dog.hotspot_clients(["192.168.4.11", "192.168.4.12"])
    assert switched == [("bb", "192.168.4.12")]
    assert dog.devices == {}
    # Nothing left: no probe any more
    dog.hotspot_clients(["192.168.4.13"])
    assert probes == ["192.168.4.11", "192.168.4.12"]


class FakeDevice:
    """urllib stand-in for one device."""

    def __init__(self, hwid: str, url: str, accept: bool = True) -> None:
        self.hwid, self.url, self.accept = hwid, url, accept
        self.calls: List[Any] = []

    def http(self, _ip, method, endpoint, payload=None, timeout=0) -> Any:
        self.calls.append((method, endpoint, payload))
        if method == "GET":
            return (
                {"hwid": self.hwid}
                if endpoint == "/device-info"
                else {"url": self.url}
            )
        if endpoint == "/cloud":
            if not self.accept:
                raise OSError("refused")
            self.url = payload["url"]
        return None

    def writes(self) -> List[str]:
        return [c[1] for c in self.calls if c[0] == "POST"]


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    monkeypatch.setattr(pw, "SETTLE_S", 0)


def test_switch_back_replays_the_validated_sequence(monkeypatch):
    dev = FakeDevice("aa", "192.168.0.10:1884")
    monkeypatch.setattr(pw, "_http", dev.http)
    assert pw.switch_back("AA", device(), "10.0.0.2")
    assert dev.writes() == ["/cloud/disable", "/cloud", "/cloud/enable"]
    written = [c[2] for c in dev.calls if c[1] == "/cloud" and c[0] == "POST"][0]
    assert written == {
        "url": "mqtt.reef-beat.com:1883",
        "api_key": "api-key",
        "api_secret": "api-secret-16-ch",
    }


def test_switch_back_writes_nothing_into_another_device(monkeypatch):
    dev = FakeDevice("zz", "192.168.0.10:1884")
    monkeypatch.setattr(pw, "_http", dev.http)
    assert not pw.switch_back("aa", device(), "10.0.0.2")
    assert dev.writes() == []


def test_switch_back_spares_a_device_already_normal(monkeypatch):
    dev = FakeDevice("aa", "mqtt://mqtt.reef-beat.com:1883/")
    monkeypatch.setattr(pw, "_http", dev.http)
    assert pw.switch_back("aa", device(), "10.0.0.2")
    assert dev.writes() == []


def test_switch_back_enables_again_after_a_refused_write(monkeypatch):
    dev = FakeDevice("aa", "192.168.0.10:1884", accept=False)
    monkeypatch.setattr(pw, "_http", dev.http)
    assert not pw.switch_back("aa", device(), "10.0.0.2")
    assert dev.writes()[-1] == "/cloud/enable"


def test_create_needs_enabled_and_a_valid_configuration():
    assert pw.create_poseidon_watchdog({}) is None
    section = {"enabled": True, "host": "ha", "key": "short"}
    assert pw.create_poseidon_watchdog({"poseidon_watchdog": section}) is None
    section["key"] = KEY
    section["cache_file"] = "/nonexistent/cache.json"
    assert pw.create_poseidon_watchdog({"poseidon_watchdog": section}) is not None
