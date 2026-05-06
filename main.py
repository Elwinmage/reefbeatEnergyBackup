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
import sys
import time
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

from monitor import create_monitor_backend
from outage import create_outage_detector, PowerState
from hotspot import NetworkManager
from controller import PumpController, OutageManager


# =============================================================================
# MQTT setup
# =============================================================================

def setup_mqtt(cfg: dict) -> Optional[mqtt.Client]:
    """Connect to MQTT broker. Returns None on failure."""
    mqtt_cfg = cfg.get("mqtt", {})
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        user = mqtt_cfg.get("user")
        if user:
            client.username_pw_set(user, mqtt_cfg.get("password"))
        host = mqtt_cfg.get("host", "localhost")
        port = mqtt_cfg.get("port", 1883)
        client.connect(host, port, keepalive=60)
        client.loop_start()
        print(f"[MQTT] Connected to {host}:{port}")
        return client
    except Exception as e:
        print(f"[MQTT] Connection failed ({e}), continuing without")
        return None


def publish_ha_discovery(client: mqtt.Client, cfg: dict):
    """Publish MQTT auto-discovery for Home Assistant."""
    mqtt_cfg = cfg.get("mqtt", {})
    device_name = mqtt_cfg.get("device_name", "reef_battery")
    base = mqtt_cfg.get("base_topic", "homeassistant")
    backend = cfg.get("monitoring", {}).get("backend", "ina226")

    device_info = {
        "identifiers": [device_name],
        "name": "Reef Battery Backup",
        "manufacturer": "KEPWORTH",
        "model": "LiFePO4 24V 60Ah",
        "sw_version": f"monitor:{backend}",
    }

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
        ("Source monitoring", "monitor_source",  "{{ value_json.monitor_source }}",       None,  None,      "mdi:chip"),
    ]

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
        client.publish(
            f"{base}/sensor/{uid_full}/config",
            json.dumps(payload), retain=True,
        )
        time.sleep(0.1)

    print(f"[MQTT] Published {len(sensors)} HA discovery configs")


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
    monitor = create_monitor_backend(cfg)
    if not monitor.initialize():
        print(f"[ERROR] Failed to initialize {monitor.name} backend")
        sys.exit(1)

    # --- MQTT ---
    mqtt_client = setup_mqtt(cfg)
    if mqtt_client:
        publish_ha_discovery(mqtt_client, cfg)

    # --- Network manager ---
    network = NetworkManager(cfg.get("network", {}))

    # --- Pump controller ---
    pump = PumpController(mqtt_client, cfg)

    # --- Outage manager ---
    outage_mgr = OutageManager(pump, network, cfg)

    # --- Outage detector ---
    detector = create_outage_detector(cfg)
    detector.on_change(outage_mgr.on_power_change)

    # Initial state check
    outage_mgr.power_state = detector.state
    if detector.state == PowerState.BATTERY:
        print("[INIT] Starting on BATTERY power!")
        outage_mgr.on_power_change(PowerState.MAINS, PowerState.BATTERY)

    print(f"[RUN] Backend={monitor.name} | Poll={poll_interval}s")
    print("-" * 70)

    try:
        while _running:
            # Read battery
            reading = monitor.read()

            # Feed monitor-based detector if used
            from outage import MonitorDetector
            if isinstance(detector, MonitorDetector):
                detector.update(reading.current)

            # Update SoC
            outage_mgr.update_soc(reading.soc)
            status = outage_mgr.get_status()

            # Runtime estimate
            runtime_h = -1.0
            if reading.current > 0.1:
                capacity = cfg.get("battery", {}).get("capacity_ah", 60.0)
                remaining = (reading.soc / 100.0) * capacity
                runtime_h = round(remaining / reading.current, 1)

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
                "monitor_source": reading.source,
            }

            # Publish MQTT
            if mqtt_client and mqtt_client.is_connected():
                topic = f"{base_topic}/sensor/{device_name}/state"
                mqtt_client.publish(topic, json.dumps(data))

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
            if runtime_h > 0:
                line += f" | ~{runtime_h}h"
            if status["outage_duration_min"] > 0:
                line += f" | outage {status['outage_duration_min']}min"
            print(line)

            time.sleep(poll_interval)

    finally:
        network.cleanup()
        detector.cleanup()
        monitor.close()
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        print("[MONITOR] Stopped")


if __name__ == "__main__":
    main()
