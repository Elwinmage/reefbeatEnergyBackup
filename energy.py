"""
reefbeat⚡Backup — Energy accumulator module.

Tracks cumulative energy (kWh) flowing in and out of the battery by
integrating INA226 power readings over time. Values are persisted to
disk so they survive service restarts.

Published sensors (state_class: total_increasing):
  - energy_charged_kwh:    total energy charged into the battery
  - energy_discharged_kwh: total energy discharged from the battery
  - energy_consumed_kwh:   total energy consumed by loads (on battery only)

All three are monotonically increasing counters that HA can use
directly in the Energy Dashboard.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional


# =============================================================================
# Energy Accumulator
# =============================================================================

class EnergyAccumulator:
    """
    Integrates power over time to produce kWh counters.

    Three counters:
      - charged:    energy flowing INTO the battery (current < 0, charger active)
      - discharged: energy flowing OUT of the battery (current > 0, on battery)
      - consumed:   energy consumed by loads while on battery (for HA Energy Dashboard)

    Persistence: values are saved to a JSON file every SAVE_INTERVAL seconds
    and on close(). On startup, values are restored from disk.
    """

    SAVE_INTERVAL = 300  # Save to disk every 5 minutes
    MIN_CURRENT = 0.1    # Ignore noise below this threshold (A)

    def __init__(self, persist_path: str = "/var/lib/reefbeat-energy-backup/energy.json"):
        self._path = Path(persist_path)
        self._last_time: Optional[float] = None
        self._last_save: float = 0

        # Cumulative counters (kWh)
        self.charged_kwh: float = 0.0
        self.discharged_kwh: float = 0.0
        self.consumed_kwh: float = 0.0

        # Load from disk
        self._load()

    def update(self, voltage: float, current: float, on_battery: bool):
        """
        Accumulate energy from a new INA226 reading.

        Args:
            voltage: battery voltage (V)
            current: battery current (A, positive = discharging)
            on_battery: True if the system is running on battery
        """
        now = time.monotonic()

        if self._last_time is not None:
            dt_h = (now - self._last_time) / 3600.0
            power_w = abs(voltage * current)
            energy_kwh = power_w * dt_h / 1000.0

            if current < -self.MIN_CURRENT:
                # Charging: current is negative (charger → battery)
                self.charged_kwh += energy_kwh

            elif current > self.MIN_CURRENT:
                # Discharging: current is positive (battery → loads)
                self.discharged_kwh += energy_kwh

                if on_battery:
                    # Only count as "consumed" when actually on battery
                    # (not when the INA226 sees normal load + charger offset)
                    self.consumed_kwh += energy_kwh

        self._last_time = now

        # Periodic save
        if now - self._last_save > self.SAVE_INTERVAL:
            self._save()
            self._last_save = now

    def get_state(self) -> dict:
        """Return current counters for MQTT publishing."""
        return {
            "energy_charged_kwh": round(self.charged_kwh, 4),
            "energy_discharged_kwh": round(self.discharged_kwh, 4),
            "energy_consumed_kwh": round(self.consumed_kwh, 4),
        }

    def close(self):
        """Save counters to disk on shutdown."""
        self._save()

    # =========================================================================
    # Persistence
    # =========================================================================

    def _load(self):
        """Restore counters from disk."""
        if not self._path.exists():
            print("[ENERGY] No saved data, starting from zero")
            return

        try:
            with open(self._path) as f:
                data = json.load(f)
            self.charged_kwh = float(data.get("charged_kwh", 0.0))
            self.discharged_kwh = float(data.get("discharged_kwh", 0.0))
            self.consumed_kwh = float(data.get("consumed_kwh", 0.0))
            print(f"[ENERGY] Restored: charged={self.charged_kwh:.3f} kWh, "
                  f"discharged={self.discharged_kwh:.3f} kWh, "
                  f"consumed={self.consumed_kwh:.3f} kWh")
        except Exception as e:
            print(f"[ENERGY] Cannot restore from {self._path}: {e}")

    def _save(self):
        """Persist counters to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "charged_kwh": self.charged_kwh,
                "discharged_kwh": self.discharged_kwh,
                "consumed_kwh": self.consumed_kwh,
                "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            # Write atomically (write to tmp then rename)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.rename(self._path)
        except Exception as e:
            print(f"[ENERGY] Cannot save to {self._path}: {e}")


# =============================================================================
# MQTT HA discovery
# =============================================================================

def publish_energy_discovery(mqtt_client, mqtt_cfg: dict):
    """Publish MQTT discovery for energy sensors."""
    device_name = mqtt_cfg.get("device_name", "reef_battery")
    base = mqtt_cfg.get("base_topic", "homeassistant")

    device_info = {
        "identifiers": [device_name],
        "name": "Reef Battery Backup",
        "manufacturer": "reefbeat Backup",
        "model": "Energy Backup System",
    }

    sensors = [
        ("energy_charged", "Energie chargee", "mdi:battery-charging",
         "energy_charged_kwh"),
        ("energy_discharged", "Energie dechargee", "mdi:battery-arrow-down",
         "energy_discharged_kwh"),
        ("energy_consumed", "Energie consommee", "mdi:lightning-bolt",
         "energy_consumed_kwh"),
    ]

    for suffix, name, icon, json_key in sensors:
        uid = f"{device_name}_{suffix}"
        discovery = {
            "name": name,
            "unique_id": uid,
            "state_topic": f"{base}/sensor/{device_name}/energy/state",
            "value_template": f"{{{{ value_json.{json_key} }}}}",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "icon": icon,
            "device": device_info,
        }
        topic = f"{base}/sensor/{uid}/config"
        mqtt_client.publish(topic, json.dumps(discovery), retain=True)

    print(f"[ENERGY] Published {len(sensors)} HA energy discovery configs")


def publish_energy_state(mqtt_client, mqtt_cfg: dict, state: dict):
    """Publish energy counters to MQTT."""
    device_name = mqtt_cfg.get("device_name", "reef_battery")
    base = mqtt_cfg.get("base_topic", "homeassistant")
    topic = f"{base}/sensor/{device_name}/energy/state"
    mqtt_client.publish(topic, json.dumps(state), retain=True)
