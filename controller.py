"""
Pump controller and outage manager.

PumpController: sends intensity commands to ReefBeat controllers
                with per-device granularity and 3 SoC-based levels
OutageManager:  orchestrates the 3-level failover response
"""

import time
import json
import threading
from typing import Optional, Dict

from outage import PowerState
from hotspot import NetworkManager, NetworkMode

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


# =============================================================================
# Intensity level resolver
# =============================================================================

class IntensityLevel:
    """
    Represents one intensity level (normal / eco / survival).
    
    Each level has:
      - soc_threshold: SoC below which this level activates
      - global_intensity: default intensity for all pumps
      - per_device: optional dict overriding intensity per pump name
    """

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.soc_threshold = cfg.get("soc_threshold", 100)
        self.global_intensity = cfg.get("global_intensity", 100)
        self.per_device: Dict[str, int] = cfg.get("per_device", {})

    def get_intensity(self, device_name: str) -> int:
        """Get intensity for a specific device, with per-device override."""
        return self.per_device.get(device_name, self.global_intensity)

    def __repr__(self):
        return (f"Level({self.name}: soc<{self.soc_threshold}%, "
                f"global={self.global_intensity}%, "
                f"overrides={self.per_device})")


class IntensityResolver:
    """
    Resolves the active intensity level based on SoC.
    
    Levels are sorted by soc_threshold descending:
      normal (soc >= 60)  -> keep original speeds
      eco    (soc >= 30)  -> reduce to save battery  
      survival (soc < 30) -> minimum for reef survival
    
    On mains power, always returns normal level.
    """

    def __init__(self, cfg: dict):
        levels_cfg = cfg.get("pump_control", {}).get("levels", {})

        self._levels = []
        for name in ["normal", "eco", "survival"]:
            if name in levels_cfg:
                self._levels.append(IntensityLevel(name, levels_cfg[name]))

        # Sort by threshold descending (normal first, survival last)
        self._levels.sort(key=lambda l: l.soc_threshold, reverse=True)

        if not self._levels:
            # Fallback defaults
            self._levels = [
                IntensityLevel("normal", {
                    "soc_threshold": 100,
                    "global_intensity": 100}),
                IntensityLevel("eco", {
                    "soc_threshold": 60,
                    "global_intensity": 50}),
                IntensityLevel("survival", {
                    "soc_threshold": 30,
                    "global_intensity": 30}),
            ]

        print("[LEVELS] Configured intensity levels:")
        for level in self._levels:
            print(f"  {level}")

    def resolve(self, soc: float, on_battery: bool) -> IntensityLevel:
        """
        Determine the active level based on SoC and power state.
        Returns the matching IntensityLevel.
        """
        if not on_battery:
            # On mains: always normal
            return self._levels[0]

        # On battery: find the level whose threshold we're below
        # Levels are sorted descending by threshold
        # Walk from highest threshold to lowest
        active = self._levels[0]  # default to normal
        for level in self._levels:
            if soc <= level.soc_threshold:
                active = level

        return active

    @property
    def normal_level(self) -> IntensityLevel:
        return self._levels[0]


# =============================================================================
# Pump controller
# =============================================================================

class PumpController:
    """
    Controls pump intensity with per-device granularity.
    
    Each pump can have its own intensity based on the active level.
    Tracks current per-device intensities to avoid redundant commands.
    """

    def __init__(self, mqtt_client, cfg: dict):
        self._client = mqtt_client
        self._cfg = cfg
        self._pump_cfg = cfg.get("pump_control", {})
        self._mqtt_cfg = cfg.get("mqtt", {})
        self._resolver = IntensityResolver(cfg)
        self._lock = threading.Lock()

        # Track per-device intensity
        self._device_intensities: Dict[str, int] = {}
        for ctrl in self._pump_cfg.get("controllers", []):
            self._device_intensities[ctrl["name"]] = 100

        # Current active level name (for status reporting)
        self.active_level_name = "normal"

    @property
    def current_intensity(self) -> int:
        """Average intensity across all devices (for status display)."""
        if not self._device_intensities:
            return 100
        vals = list(self._device_intensities.values())
        return round(sum(vals) / len(vals))

    def apply_level(self, soc: float, on_battery: bool, reason: str = ""):
        """
        Determine the appropriate level and apply per-device intensities.
        Only sends commands for devices whose intensity actually changed.
        """
        level = self._resolver.resolve(soc, on_battery)

        with self._lock:
            if level.name == self.active_level_name and reason == "":
                return  # No change

            old_level = self.active_level_name
            self.active_level_name = level.name
            controllers = self._pump_cfg.get("controllers", [])

            # Determine per-device targets
            changes = []
            for ctrl in controllers:
                name = ctrl["name"]
                target = level.get_intensity(name)
                current = self._device_intensities.get(name, -1)

                if target != current:
                    changes.append((ctrl, current, target))
                    self._device_intensities[name] = target

            if not changes and old_level == level.name:
                return

            # Log level change
            if old_level != level.name:
                print(f"[PUMPS] Level: {old_level} -> {level.name} "
                      f"(SoC={soc:.0f}%, {reason})")

            # Apply changes
            for ctrl, old_val, new_val in changes:
                name = ctrl["name"]
                print(f"  [PUMP] {name}: {old_val}% -> {new_val}%")
                self._api_set(ctrl, new_val)

            # MQTT: publish per-device state
            self._publish_pump_state(level, reason)

    def restore_normal(self):
        """Restore all pumps to normal level."""
        normal = self._resolver.normal_level
        with self._lock:
            self.active_level_name = "normal"
            controllers = self._pump_cfg.get("controllers", [])

            for ctrl in controllers:
                name = ctrl["name"]
                target = normal.get_intensity(name)
                current = self._device_intensities.get(name, -1)
                if target != current:
                    print(f"  [PUMP] {name}: {current}% -> {target}% (restored)")
                    self._api_set(ctrl, target)
                    self._device_intensities[name] = target

            self._publish_pump_state(normal, "power_restored")

    def _api_set(self, ctrl: dict, intensity: int):
        """
        Send intensity command to a ReefBeat controller.
        TODO: Replace with actual ReefBeat controller API.
        """
        if not REQUESTS_AVAILABLE:
            return
        ip = ctrl.get("ip")
        name = ctrl.get("name", "unknown")
        if not ip:
            return
        try:
            resp = requests.post(
                f"http://{ip}/api/pump/intensity",
                json={"intensity": intensity}, timeout=3,
            )
            status = "OK" if resp.ok else f"FAIL({resp.status_code})"
            print(f"    [API] {name}@{ip} -> {intensity}% {status}")
        except requests.exceptions.RequestException as e:
            print(f"    [API] {name}@{ip} -> unreachable "
                  f"({type(e).__name__})")

    def _publish_pump_state(self, level: IntensityLevel, reason: str):
        """Publish per-device pump state to MQTT for HA."""
        if not self._client or not self._client.is_connected():
            return

        device = self._mqtt_cfg.get("device_name", "reef_battery")
        base = self._mqtt_cfg.get("base_topic", "homeassistant")
        topic = f"{base}/sensor/{device}/pump_command"

        payload = {
            "command": "set_intensity",
            "level": level.name,
            "global_intensity": level.global_intensity,
            "per_device": dict(self._device_intensities),
            "reason": reason,
            "timestamp": time.time(),
        }
        self._client.publish(topic, json.dumps(payload), retain=True)


# =============================================================================
# Outage manager with 3-level failover
# =============================================================================

class OutageManager:
    """
    Central decision engine for outage response.
    
    Outage sequence:
      1. Outage detected (relay or monitor)
      2. Wait configurable delay (for router UPS to stabilize)
      3. Execute 3-level network failover
      4. Apply pump intensity based on SoC level
    
    During outage:
      - Continuously monitors SoC
      - Adjusts pump intensity as SoC drops through thresholds
      - normal -> eco -> survival (graduated response)
    
    Power restore:
      1. Restore detected
      2. Restore network
      3. Restore all pumps to normal level
    """

    def __init__(self, pump: PumpController,
                 network: NetworkManager, cfg: dict):
        self._pump = pump
        self._network = network
        self._cfg = cfg
        self._pump_cfg = cfg.get("pump_control", {})
        self._failover_cfg = cfg.get("network", {}).get("failover", {})
        self.power_state = PowerState.MAINS
        self.soc = 100.0
        self.outage_start: Optional[float] = None
        self._failover_thread: Optional[threading.Thread] = None
        self._stop_failover = threading.Event()

    def on_power_change(self, old: PowerState, new: PowerState):
        """Called when power state changes."""
        self.power_state = new

        if new == PowerState.BATTERY:
            self.outage_start = time.monotonic()
            print("[OUTAGE] === POWER OUTAGE DETECTED ===")
            self._stop_failover.clear()
            self._failover_thread = threading.Thread(
                target=self._failover_sequence, daemon=True
            )
            self._failover_thread.start()

        else:
            duration = 0.0
            if self.outage_start:
                duration = (time.monotonic() - self.outage_start) / 60.0
            self.outage_start = None
            print(f"[OUTAGE] === POWER RESTORED ({duration:.1f} min) ===")

            # Stop failover
            self._stop_failover.set()

            # Restore network
            self._network.restore_normal()

            # Restore all pumps to normal
            self._pump.restore_normal()

    def _failover_sequence(self):
        """Background: wait, network failover, apply pump levels."""
        delay = self._failover_cfg.get("check_delay_s", 30.0)
        print(f"[FAILOVER] Waiting {delay}s for network stabilization...")

        if self._stop_failover.wait(timeout=delay):
            print("[FAILOVER] Cancelled (power restored during wait)")
            return

        # Network failover (3 levels)
        controllers = self._pump_cfg.get("controllers", [])
        reached = self._network.execute_failover(
            controllers, self._stop_failover
        )

        if self._stop_failover.is_set():
            return

        if reached:
            print("[FAILOVER] Controllers reachable")
        else:
            print("[FAILOVER] Some controllers may be unreachable")

        # Apply battery level based on current SoC
        self._pump.apply_level(
            self.soc, on_battery=True, reason="outage_initial"
        )

        # Monitor loop
        check_interval = self._failover_cfg.get(
            "router_check_interval_s", 60.0)
        while not self._stop_failover.is_set():
            if self._stop_failover.wait(timeout=check_interval):
                return

    def update_soc(self, soc: float):
        """Update SoC and adjust pump levels if on battery."""
        old_soc = self.soc
        self.soc = soc
        if self.power_state == PowerState.BATTERY:
            self._pump.apply_level(soc, on_battery=True)

    def get_status(self) -> dict:
        outage_min = 0.0
        if self.outage_start:
            outage_min = round(
                (time.monotonic() - self.outage_start) / 60.0, 1)
        return {
            "power_state": self.power_state.value,
            "pump_intensity": self._pump.current_intensity,
            "pump_level": self._pump.active_level_name,
            "pump_details": dict(self._pump._device_intensities),
            "outage_duration_min": outage_min,
            "network_mode": self._network.mode.value,
        }
