#!/usr/bin/env python3
"""
Reef Battery Monitor — Main entry point.

Loads config from config.json, initializes the selected monitoring
backend and outage detection method, then runs the main loop.

Usage:
  python3 main.py                       # Default config.json
  python3 main.py /path/to/config.json  # Custom config path
"""

import json
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional


def _get_local_ip() -> str:
    """Get the RPi's local IP address (primary interface)."""
    try:
        # Connect to a public DNS to determine the local IP
        # (doesn't actually send data, just binds the socket)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"

import paho.mqtt.client as mqtt

from monitor import create_monitor_backend
from outage import create_outage_detector, PowerState
from hotspot import NetworkManager
from controller import PumpController, OutageManager
from mqtt_buffer import MqttBuffer
from notifier import create_notifier
from updater import create_updater
from lte_monitor import create_lte_monitor
from energy import EnergyAccumulator, publish_energy_discovery, publish_energy_state
from maintenance import BatteryTestMaintenance
from device_info import build_device_info
from test_controls import TestControls


# =============================================================================
# MQTT setup
# =============================================================================

def setup_mqtt(cfg: dict, buffer: "MqttBuffer") -> Optional[mqtt.Client]:
    """Configure the MQTT client with a non-blocking connection.

    During a power outage the broker (and HA) typically die a few
    minutes after we've lost the mains, but our service keeps running.
    We must therefore tolerate:
      - broker down at startup     -> use connect_async so we don't block
      - broker dropping mid-run    -> rely on paho's reconnect_delay_set
      - broker coming back later   -> wake the buffer to replay pending msgs

    The buffer is wired into on_connect / on_disconnect callbacks so the
    replay thread reacts immediately to state changes instead of polling.
    """
    mqtt_cfg = cfg.get("mqtt", {})
    host = mqtt_cfg.get("host", "localhost")
    port = mqtt_cfg.get("port", 1883)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    user = mqtt_cfg.get("user")
    if user:
        client.username_pw_set(user, mqtt_cfg.get("password"))

    # Reconnect logic: paho will retry between min and max seconds with
    # exponential backoff. We pick a fairly aggressive 1->60s curve so
    # we re-establish quickly when the broker (or its host) comes back.
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    def _on_connect(c, userdata, flags, rc, props=None):
        if rc == 0:
            print(f"[MQTT] Connected to {host}:{port}")
            buffer.notify_connected()
        else:
            print(f"[MQTT] Connection refused (rc={rc})")

    def _on_disconnect(c, userdata, *args, **kwargs):
        # paho calls this on graceful disconnect AND on broker drop.
        # We log it so the user can correlate with the journal.
        print(f"[MQTT] Disconnected -- buffering until reconnection")

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect

    # connect_async: returns immediately even if the broker is down.
    # The actual connection attempt happens inside loop_start()'s thread
    # and is automatically retried until success.
    try:
        client.connect_async(host, port, keepalive=60)
        client.loop_start()
        print(f"[MQTT] Trying {host}:{port} (non-blocking)...")
    except Exception as e:  # noqa: BLE001
        # connect_async should not raise for normal network issues; if
        # it does (e.g. invalid hostname), there's no point retrying.
        print(f"[MQTT] Setup failed ({e}), continuing without MQTT")
        return None

    return client


def publish_ha_discovery(buffer: "MqttBuffer", cfg: dict,
                         has_victron: bool = False):
    """Publish MQTT auto-discovery for Home Assistant.

    Goes through the file-backed buffer so that if HA is down (e.g.
    same outage that triggered the failover), the discovery configs
    are replayed once the broker comes back. retain=True ensures HA
    picks them up even if it joined after the publish.

    has_victron : when True, also publishes auxiliary charger sensors fed
                  by the Victron BLE auxiliary backend.
    """
    mqtt_cfg = cfg.get("mqtt", {})
    device_name = mqtt_cfg.get("device_name", "reef_battery")
    base = mqtt_cfg.get("base_topic", "homeassistant")
    backend = cfg.get("monitoring", {}).get("backend", "ina226")

    device_info = build_device_info(device_name, f"monitor:{backend}")

    sensors = [
        ("Tension batterie",  "voltage",        "{{ value_json.voltage }}",              "V",   "voltage", "mdi:flash"),
        ("Courant batterie",  "current",         "{{ value_json.current }}",              "A",   "current", "mdi:current-dc"),
        ("Puissance",         "power",           "{{ value_json.power }}",                "W",   "power",   "mdi:flash-outline"),
        ("SoC batterie",      "soc",             "{{ value_json.soc }}",                  "%",   "battery", "mdi:battery"),
        ("État secteur",      "power_state",     "{{ value_json.power_state }}",          None,  None,      "mdi:power-plug"),
        ("Intensité pompes",  "pump_intensity",  "{{ value_json.pump_intensity }}",       "%",   None,      "mdi:pump"),
        ("Autonomie",         "runtime",         "{{ value_json.runtime_h }}",            "h",   None,      "mdi:timer-sand"),
        ("Durée coupure",     "outage_duration", "{{ value_json.outage_duration_min }}",  "min", None,      "mdi:clock-alert-outline"),
        ("Mode réseau",       "network_mode",    "{{ value_json.network_mode }}",         None,  None,      "mdi:wifi"),
        ("IP locale",         "local_ip",        "{{ value_json.local_ip }}",             None,  None,      "mdi:ip-network"),
        ("Source monitoring", "monitor_source",  "{{ value_json.monitor_source }}",       None,  None,      "mdi:chip"),
    ]

    # Charger telemetry (only when a Victron auxiliary is configured).
    # These sensors have an availability_template that checks if the
    # charger data is present in the payload. When the Victron BLE is
    # not responding or not polled yet, the sensors show as
    # "unavailable" instead of trying to parse a missing numeric value.
    charger_avail_tpl = "{{ 'online' if value_json.charger_voltage is defined and value_json.charger_voltage is not none else 'offline' }}"
    if has_victron:
        sensors += [
            ("Tension chargeur",  "charger_voltage", "{{ value_json.charger_voltage }}", "V",  "voltage", "mdi:ev-station"),
            ("Courant chargeur",  "charger_current", "{{ value_json.charger_current }}", "A",  "current", "mdi:current-ac"),
            ("Puissance chargeur", "charger_power",  "{{ (value_json.charger_voltage * value_json.charger_current) | round(1) }}", "W", "power", "mdi:flash"),
            ("État chargeur",     "charger_state",   "{{ value_json.charger_state | default('unknown') }}",   None, None,      "mdi:battery-charging"),
            ("Erreur chargeur",   "charger_error",   "{{ value_json.charger_error | default('unknown') }}",   None, None,      "mdi:alert-circle-outline"),
        ]

    # Track which UIDs are charger sensors (need availability template)
    charger_uids = {"charger_voltage", "charger_current", "charger_power", "charger_state", "charger_error"}

    # Stable role markers. The reef_battery_test blueprint and ha-reef-card
    # look these up by attribute, which survives the user renaming an entity
    # (matching on entity_id would not).
    roles = {
        "soc": "battery_soc",
        "voltage": "battery_voltage",
        "power": "battery_power",
    }

    for name, uid, tpl, unit, dc, icon in sensors:
        uid_full = f"{device_name}_{uid}"
        payload = {
            "name": name,
            "unique_id": uid_full,
            "state_topic": f"{base}/sensor/{device_name}/state",
            "value_template": tpl,
            "icon": icon,
            "device": device_info,
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if dc:
            payload["device_class"] = dc
        if uid in roles:
            # json_attributes_template only has to render a JSON object; a
            # constant one is what publishes the marker.
            payload["json_attributes_topic"] = (
                f"{base}/sensor/{device_name}/state")
            payload["json_attributes_template"] = json.dumps(
                {"reef_role": roles[uid]})
        # Add availability for charger sensors
        if uid in charger_uids:
            payload["availability_topic"] = f"{base}/sensor/{device_name}/state"
            payload["availability_template"] = charger_avail_tpl
        buffer.publish(
            f"{base}/sensor/{uid_full}/config",
            json.dumps(payload), retain=True,
        )
        # Throttle slightly so we don't overwhelm the buffer's flush
        # cadence on slow SD cards.
        time.sleep(0.05)

    print(f"[MQTT] Queued {len(sensors)} HA discovery configs")

    # Energy accumulator discovery
    publish_energy_discovery(buffer, mqtt_cfg)


# =============================================================================
# Main
# =============================================================================

_running = True


def _signal_handler(sig, frame):
    global _running
    print("\n[INFO] Shutting down...")
    _running = False


def load_config(path: str) -> dict:
    """Load and validate config from JSON file."""
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] Config not found: {path}")
        print("[ERROR] Copy config.json to the working directory and edit it")
        sys.exit(1)

    with open(p) as f:
        cfg = json.load(f)

    monitoring = cfg.get("monitoring", {})
    detection = cfg.get("outage_detection", {})
    network = cfg.get("network", {})
    failover = network.get("failover", {})

    print(f"[CONFIG] Loaded from {path}")
    print(f"  Monitoring backend  : {monitoring.get('backend', '?')}")
    print(f"  Outage detection    : {detection.get('method', '?')}")
    print(f"  Battery capacity    : {cfg.get('battery', {}).get('capacity_ah', '?')} Ah")
    print(f"  Network failover    : {'enabled' if failover.get('enabled') else 'disabled'}")
    print(f"  Home Wi-Fi SSID     : {network.get('home_wifi', {}).get('ssid', '?')}")
    return cfg


def main():
    global _running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Load config
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_config(config_path)

    poll_interval = cfg.get("poll_interval_s", 5.0)
    mqtt_cfg = cfg.get("mqtt", {})
    device_name = mqtt_cfg.get("device_name", "reef_battery")
    base_topic = mqtt_cfg.get("base_topic", "homeassistant")

    # --- Monitoring backend ---
    # INA226 is the primary battery monitor (mandatory). Victron BLE is an
    # optional auxiliary that adds charger telemetry to the MQTT payload
    # but never gates the service on its availability.
    monitor, victron_aux = create_monitor_backend(cfg)
    if not monitor.initialize():
        print(f"[ERROR] Failed to initialize {monitor.name} backend")
        sys.exit(1)
    if victron_aux:
        print(f"[INFO] Auxiliary backend active: {victron_aux.name}")

    # --- MQTT (with file-backed buffer for outage replay) ---
    # The buffer survives broker outages by writing every message to
    # disk first; the replay thread pushes them to the broker as soon
    # as it's reachable. This means we never lose data points -- HA
    # gets a continuous timeline even if it died for hours during the
    # very outage we're trying to ride out.
    buffer_dir = Path(cfg.get("mqtt", {}).get(
        "buffer_dir", "/var/lib/reefbeat-energy-backup/mqtt"
    ))
    retention_days = cfg.get("mqtt", {}).get("buffer_retention_days", 7)
    mqtt_buffer = MqttBuffer(buffer_dir, retention_days=retention_days)

    mqtt_client = setup_mqtt(cfg, mqtt_buffer)
    if mqtt_client:
        mqtt_buffer.attach_client(mqtt_client)
        publish_ha_discovery(mqtt_buffer, cfg,
                             has_victron=victron_aux is not None)

    # --- Network manager ---
    network = NetworkManager(cfg.get("network", {}))

    # --- Pump controller ---
    pump = PumpController(mqtt_client, cfg)

    # --- Notifier (ntfy.sh + LTE failover) ---
    notifier = create_notifier(cfg)

    # --- Outage manager ---
    outage_mgr = OutageManager(pump, network, cfg, notifier=notifier)

    # --- Updater (background GitHub version check + HA update entity) ---
    install_dir = str(Path(__file__).parent)
    updater = create_updater(cfg, mqtt_client, install_dir)
    updater.start()

    # --- LTE Monitor (4G modem status + MQTT sensors) ---
    lte_monitor = create_lte_monitor(cfg, mqtt_client)
    lte_monitor.start()

    # --- Maintenance task (battery test, surfaced in ha-reef-card) ---
    maintenance = None
    if mqtt_client:
        maintenance = BatteryTestMaintenance(
            mqtt_client,
            cfg.get("mqtt", {}),
            build_device_info(
                cfg.get("mqtt", {}).get("device_name", "reef_battery")),
            persist_path=cfg.get("maintenance", {}).get(
                "persist_path",
                "/var/lib/reefbeat-energy-backup/maintenance.json"),
        )
        maintenance.start()

    # --- Functional test controls (pump command test, Wi-Fi cut) ---
    test_controls = None
    if mqtt_client:
        test_controls = TestControls(
            mqtt_client,
            cfg.get("mqtt", {}),
            build_device_info(
                cfg.get("mqtt", {}).get("device_name", "reef_battery")),
            pump, network,
        )
        test_controls.start()

    # --- Outage detector ---
    detector = create_outage_detector(cfg)
    detector.on_change(outage_mgr.on_power_change)

    # --- Reconcile after a possible mid-outage reboot ---
    # If snapshots remain on disk from a previous run, the Pi may have
    # crashed/rebooted while we were overriding pump schedules. Replay
    # them now (or wait for power to come back, depending on detector state).
    on_battery_now = detector.state == PowerState.BATTERY
    pump.reconcile_on_startup(on_battery=on_battery_now)

    # Initial state check
    outage_mgr.power_state = detector.state
    if detector.state == PowerState.BATTERY:
        print("[INIT] Starting on BATTERY power!")
        outage_mgr.on_power_change(PowerState.MAINS, PowerState.BATTERY)

    print(f"[RUN] Backend={monitor.name} | Poll={poll_interval}s")
    print("-" * 70)

    try:
        # The Victron BLE scan costs 1-3s per call -- too expensive to do
        # every cycle. Poll it once every N cycles, and keep the most
        # recent reading available between polls so the log line and MQTT
        # payload always show charger telemetry, not just on poll cycles.
        victron_every_n = max(1, cfg.get("victron_poll_every_n", 6))
        victron_tick = 0
        last_charger: Optional[dict] = None  # last known good charger data

        # Runtime-estimate filter: a sliding window of recent battery
        # currents lets us smooth out the INA226 noise (a few hundred mA
        # of jitter at low load) before computing autonomy. ~60 seconds
        # of history gives a stable enough number to be useful while
        # still reacting reasonably fast to a real outage.
        from collections import deque
        runtime_window_s = cfg.get("runtime_window_s", 60.0)
        runtime_window_n = max(3, int(runtime_window_s / poll_interval))
        current_history: deque = deque(maxlen=runtime_window_n)

        # Cached theoretical runtime when on mains — computed once, then
        # stable. Reset to None when switching to battery so it gets
        # recomputed when mains returns.
        _mains_runtime_h: Optional[float] = None
        _prev_on_mains: bool = True

        # Energy accumulator (kWh counters with disk persistence)
        energy = EnergyAccumulator()
        energy_publish_counter = 0
        energy_publish_every = 12  # Publish every 12 cycles (~60s at 5s poll)

        while _running:
            # Read battery (INA226 is fast and reliable)
            on_mains = outage_mgr.power_state == PowerState.MAINS

            # Reset cached runtime when power state changes
            if on_mains != _prev_on_mains:
                _mains_runtime_h = None
                _prev_on_mains = on_mains

            reading = monitor.read(on_mains=on_mains)

            # Refresh charger telemetry once every N cycles. Between polls
            # we re-inject the previous reading so the user sees a stable
            # value rather than alternating "with/without" data.
            if victron_aux is not None:
                victron_tick += 1
                if victron_tick >= victron_every_n:
                    victron_tick = 0
                    aux_data = victron_aux.read()
                    if aux_data is not None:
                        last_charger = aux_data
                    # If aux_data is None (timeout, BLE noise), keep the
                    # previous last_charger -- the data is at most
                    # N*poll_interval seconds old, which is fine for
                    # charger-state monitoring.
                if last_charger is not None:
                    reading.charger_voltage = last_charger["voltage"]
                    reading.charger_current = last_charger["current"]
                    reading.charger_state = last_charger["state"]
                    reading.charger_error = last_charger["error"]
                    reading.charger_source = last_charger["source"]

            # Feed monitor-based detector if used
            from outage import MonitorDetector
            if isinstance(detector, MonitorDetector):
                detector.update(reading.current)

            # Runtime estimate.
            #
            # Two modes:
            # - On battery: use real measured current (averaged over a
            #   sliding window to smooth INA226 jitter).
            # - On mains: use theoretical consumption from pump power
            #   tables with FULL battery capacity (not SoC-dependent).
            #   On mains the battery is maintained at 100% by the charger,
            #   so the runtime should be a stable constant: "total Ah /
            #   estimated current draw". Any SoC drift from measurement
            #   noise must NOT affect this value.
            current_history.append(reading.current)
            runtime_h = None

            capacity = cfg.get("battery", {}).get("capacity_ah", 60.0)

            if outage_mgr.power_state == PowerState.BATTERY:
                # Real measurement: average current over sliding window
                remaining_ah = (reading.soc / 100.0) * capacity
                avg_current = sum(current_history) / len(current_history)
                if avg_current > 0.2:
                    runtime_h = round(remaining_ah / avg_current, 1)
            else:
                # On mains: battery is full, use 100% capacity
                # Runtime = total usable capacity / theoretical draw
                # This is a FIXED value that does not change over time
                if _mains_runtime_h is None:
                    try:
                        from power_estimation import device_power_at
                        total_watts = 0.0
                        for ctrl in cfg.get("pump_control", {}).get("controllers", []):
                            pump_model = ctrl.get("pump_model", "")
                            w = device_power_at(pump_model, 100)
                            total_watts += w
                        # Add RPi consumption (~3.5W)
                        total_watts += 3.5
                        if total_watts > 0:
                            nominal_voltage = 25.6  # LiFePO4 24V nominal
                            usable_ah = capacity * 0.8  # 80% DoD for LiFePO4
                            estimated_current = total_watts / nominal_voltage
                            _mains_runtime_h = round(usable_ah / estimated_current, 1)
                            print(f"[RUNTIME] Theoretical: {total_watts:.0f}W "
                                  f"→ {estimated_current:.1f}A "
                                  f"→ {_mains_runtime_h}h autonomy")
                    except Exception as e:
                        print(f"[RUNTIME] Cannot compute theoretical: {e}")
                        _mains_runtime_h = -1.0
                runtime_h = _mains_runtime_h if _mains_runtime_h and _mains_runtime_h > 0 else None

            # Update SoC (may trigger notifications via notifier)
            outage_mgr.update_soc(reading.soc, runtime_h=runtime_h if runtime_h else -1.0)
            status = outage_mgr.get_status()

            # State payload
            data = {
                "voltage": reading.voltage,
                "current": reading.current,
                "power": reading.power,
                "soc": reading.soc,
                "power_state": status["power_state"],
                "pump_intensity": status["pump_intensity"],
                "runtime_h": runtime_h,
                "outage_duration_min": status["outage_duration_min"],
                "network_mode": status["network_mode"],
                "local_ip": _get_local_ip(),
                "monitor_source": reading.source,
            }
            # Add charger fields only when present (avoids publishing
            # nulls to MQTT for users who haven't configured Victron).
            if reading.charger_source is not None:
                data["charger_voltage"] = reading.charger_voltage
                data["charger_current"] = reading.charger_current
                data["charger_state"] = reading.charger_state
                data["charger_error"] = reading.charger_error

            # Publish to the buffer. This always succeeds locally: if
            # the broker is up the message is forwarded immediately;
            # if it's down, it's stored and replayed on reconnection.
            # No "if mqtt_client.is_connected()" gate -- we WANT to
            # capture the data points even (especially!) during an
            # outage when HA is dead.
            if mqtt_client is not None:
                topic = f"{base_topic}/sensor/{device_name}/state"
                mqtt_buffer.publish(topic, json.dumps(data))

            # Energy accumulation
            on_battery_now = outage_mgr.power_state == PowerState.BATTERY
            energy.update(reading.voltage, reading.current, on_battery_now)

            # Publish energy counters periodically (every ~60s)
            energy_publish_counter += 1
            if energy_publish_counter >= energy_publish_every and mqtt_client:
                energy_publish_counter = 0
                publish_energy_state(
                    mqtt_buffer, mqtt_cfg, energy.get_state())
                if maintenance:
                    maintenance.publish_state()
                if test_controls:
                    test_controls.publish_state()

            # Console
            pwr = "⚡" if status["power_state"] == "mains" else "🔋"
            net_icons = {
                "client": "🌐", "rejoin": "🔄",
                "hotspot": "📡", "unknown": "❓",
            }
            net = net_icons.get(status["network_mode"], "❓")
            src = f"[{reading.source}]"
            line = (
                f"  {pwr}{net} {src:10s} "
                f"{reading.voltage:5.2f}V | {reading.current:+6.3f}A | "
                f"{reading.power:5.1f}W | SoC {reading.soc:4.1f}% | "
                f"Pumps {status['pump_intensity']:3d}%"
            )
            if runtime_h is not None and runtime_h > 0:
                line += f" | ~{runtime_h}h"
            if status["outage_duration_min"] > 0:
                line += f" | outage {status['outage_duration_min']}min"
            # Append charger telemetry whenever it's available. We show
            # voltage + current + state so the user sees the charger's
            # behaviour at a glance: e.g. "chrg 27.0V +1.5A storage".
            if reading.charger_state is not None:
                cv = reading.charger_voltage
                cc = reading.charger_current
                line += " | chrg"
                if cv is not None:
                    line += f" {cv:5.2f}V"
                if cc is not None:
                    line += f" {cc:+.2f}A"
                line += f" {reading.charger_state}"
            print(line)

            time.sleep(poll_interval)

    finally:
        energy.close()
        lte_monitor.stop()
        updater.stop()
        network.cleanup()
        detector.cleanup()
        monitor.close()
        if victron_aux:
            victron_aux.close()
        # Stop the buffer FIRST so the replay thread doesn't try to
        # publish through a client we're about to disconnect.
        mqtt_buffer.stop()
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        print("[MONITOR] Stopped")


if __name__ == "__main__":
    main()
