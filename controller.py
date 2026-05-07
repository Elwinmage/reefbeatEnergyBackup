"""
Pump controller and outage manager.

PumpController: sends intensity commands to ReefBeat controllers
                with per-device granularity and 3 SoC-based levels
OutageManager:  orchestrates the 3-level failover response
"""

import time
import json
import os
import threading
from pathlib import Path
from typing import Optional, Dict, Any

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
      - per_device: optional dict overriding intensity per pump key
                    (key = ctrl["key"], unique per controllable pump;
                     for RSRUN this is "<device_name>::pump_1" / "pump_2")

    Special value: 0 means "turn the pump OFF" at that level.
    """

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.soc_threshold = cfg.get("soc_threshold", 100)
        self.global_intensity = cfg.get("global_intensity", 100)
        self.per_device: Dict[str, int] = cfg.get("per_device", {})

    def get_intensity(self, pump_key: str) -> int:
        """Get intensity for a specific pump (by unique key), with override."""
        return self.per_device.get(pump_key, self.global_intensity)

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
    Controls pump intensity with per-pump granularity.

    Each pump can have its own intensity based on the active level.
    For multi-pump devices (RSRUN: return + skimmer share one box+IP),
    each pump is tracked and addressed independently via its `pump_index`
    ("pump_1", "pump_2", ...).

    Identification:
      - Every controllable pump has a unique `key` (set by the wizard):
          * single-pump devices (RSWAVE, RSLED): key == device name
          * multi-pump RSRUN:                    key == "<name>::pump_1"
      - `_device_intensities` and per_device overrides are indexed by key.

    Intensity semantics:
      - 0 means OFF (RSWAVE: stop; RSRUN: schedule_enabled=false on that pump)
      - otherwise the value must respect the model's running range; the wizard
        already validates this so the controller just forwards what it gets.
    """

    def __init__(self, mqtt_client, cfg: dict):
        self._client = mqtt_client
        self._cfg = cfg
        self._pump_cfg = cfg.get("pump_control", {})
        self._mqtt_cfg = cfg.get("mqtt", {})
        self._resolver = IntensityResolver(cfg)
        self._lock = threading.Lock()

        # Track per-pump intensity, indexed by unique pump key
        self._device_intensities: Dict[str, int] = {}
        for ctrl in self._pump_cfg.get("controllers", []):
            self._device_intensities[ctrl["key"]] = 100

        # Current active level name (for status reporting)
        self.active_level_name = "normal"

    @property
    def current_intensity(self) -> int:
        """Average intensity across all pumps (for status display)."""
        if not self._device_intensities:
            return 100
        vals = list(self._device_intensities.values())
        return round(sum(vals) / len(vals))

    def apply_level(self, soc: float, on_battery: bool, reason: str = ""):
        """
        Determine the appropriate level and apply per-pump intensities.
        Only sends commands for pumps whose intensity actually changed.
        """
        level = self._resolver.resolve(soc, on_battery)

        with self._lock:
            if level.name == self.active_level_name and reason == "":
                return  # No change

            old_level = self.active_level_name
            self.active_level_name = level.name
            controllers = self._pump_cfg.get("controllers", [])

            # Determine per-pump targets
            changes = []
            for ctrl in controllers:
                key = ctrl["key"]
                target = level.get_intensity(key)
                current = self._device_intensities.get(key, -1)

                if target != current:
                    changes.append((ctrl, current, target))
                    self._device_intensities[key] = target

            if not changes and old_level == level.name:
                return

            # Log level change
            if old_level != level.name:
                print(f"[PUMPS] Level: {old_level} -> {level.name} "
                      f"(SoC={soc:.0f}%, {reason})")

            # Apply changes
            for ctrl, old_val, new_val in changes:
                label = self._ctrl_label(ctrl)
                print(f"  [PUMP] {label}: {old_val}% -> {new_val}%")
                self._api_set(ctrl, new_val)

            # MQTT: publish per-pump state
            self._publish_pump_state(level, reason)

    def restore_normal(self):
        """
        Restore all pumps to their pre-outage configuration.

        Each pump that was overridden during the outage has a snapshot on
        disk; we re-push that snapshot so the user's original schedule
        (RSRUN) or wave program (RSWAVE) comes back exactly as it was.
        Pumps that were never overridden keep running untouched.

        We do NOT just push 100% via _api_set: that would replace the
        user's daily schedule with a flat one-slot or uniform wave.
        """
        normal = self._resolver.normal_level
        with self._lock:
            self.active_level_name = "normal"
            controllers = self._pump_cfg.get("controllers", [])

            for ctrl in controllers:
                key = ctrl["key"]
                # Only restore if we actually overrode this pump (i.e. a
                # snapshot is present on disk). Otherwise nothing to do.
                if self._load_snapshot(key) is not None:
                    label = self._ctrl_label(ctrl)
                    print(f"  [PUMP] {label}: restoring original config")
                    self._api_restore(ctrl)
                # Either way, the pump is now back to normal target.
                self._device_intensities[key] = normal.get_intensity(key)

            self._publish_pump_state(normal, "power_restored")

    def reconcile_on_startup(self, on_battery: bool) -> None:
        """
        Called at service startup. Handles the case where the Pi rebooted
        mid-outage and we still have snapshots on disk.

        - If we are back on mains power (on_battery=False) and snapshots
          exist, the outage ended while we were down: restore everything.
        - If we are still on battery, leave snapshots in place; the next
          apply_level/restore_normal cycle will use them.
        """
        controllers = self._pump_cfg.get("controllers", [])
        stale = [c for c in controllers
                 if self._load_snapshot(c["key"]) is not None]

        if not stale:
            return

        if on_battery:
            print(f"[STARTUP] {len(stale)} stale snapshot(s) found, "
                  "still on battery -- keeping them")
            return

        print(f"[STARTUP] {len(stale)} stale snapshot(s) found and mains "
              "is back -- restoring originals")
        self.restore_normal()

    @staticmethod
    def _ctrl_label(ctrl: dict) -> str:
        """Human-readable label for a pump entry (used in logs)."""
        if ctrl.get("pump_index"):
            sub = ctrl.get("pump_name") or ctrl["pump_index"]
            return f"{ctrl['name']} / {sub}"
        return ctrl["name"]

    # -------------------------------------------------------------------------
    # Snapshot persistence
    # -------------------------------------------------------------------------
    # Snapshots are kept on disk so that an unplanned reboot of the Pi during
    # an outage does not destroy the original schedule/wave configuration.
    # On startup, if a snapshot file exists, we know we crashed mid-outage
    # and the device may still be running our reduced schedule -- the
    # snapshot lets us push back the original config when the mains return.

    @property
    def _snapshot_dir(self) -> Path:
        """Where to persist per-pump original configuration."""
        # Honour an explicit override; otherwise put it next to the config.
        path = self._cfg.get("snapshot_dir")
        if path:
            return Path(path)
        return Path("/var/lib/reef-battery-monitor/snapshots")

    def _snapshot_path(self, key: str) -> Path:
        """Return the snapshot file path for a given pump key."""
        # Sanitise the key for filesystem usage (":" is fine on ext4 but ugly)
        safe = key.replace("/", "_").replace(":", "-")
        return self._snapshot_dir / f"{safe}.json"

    def _save_snapshot(self, key: str, snapshot: Dict[str, Any]) -> None:
        """Persist a snapshot atomically (tmp + rename)."""
        try:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            path = self._snapshot_path(key)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshot, indent=2))
            os.replace(tmp, path)
        except OSError as e:
            print(f"    [SNAP] failed to save {key}: {e}")

    def _load_snapshot(self, key: str) -> Optional[Dict[str, Any]]:
        """Load a snapshot from disk if present."""
        path = self._snapshot_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"    [SNAP] failed to load {key}: {e}")
            return None

    def _drop_snapshot(self, key: str) -> None:
        """Remove a snapshot file (after successful restore)."""
        try:
            self._snapshot_path(key).unlink(missing_ok=True)
        except OSError:
            pass

    # -------------------------------------------------------------------------
    # ReefBeat HTTP primitives
    # -------------------------------------------------------------------------
    # These mirror what the ha-reefbeat custom component does, but called
    # directly so we never depend on Home Assistant being up during an outage.

    def _http_get(self, ip: str, path: str) -> Optional[Any]:
        """GET <path> from a ReefBeat device, returns parsed JSON or None."""
        if not REQUESTS_AVAILABLE:
            return None
        try:
            r = requests.get(f"http://{ip}{path}", timeout=3)
            if r.ok:
                return r.json()
            print(f"    [HTTP] GET {ip}{path} -> {r.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"    [HTTP] GET {ip}{path} -> unreachable ({type(e).__name__})")
        return None

    def _http_send(self, ip: str, path: str, payload: Any = "",
                   method: str = "put") -> bool:
        """Send a request (PUT/POST/DELETE) to a ReefBeat device."""
        if not REQUESTS_AVAILABLE:
            return False
        url = f"http://{ip}{path}"
        try:
            if method == "put":
                r = requests.put(url, json=payload, timeout=5)
            elif method == "post":
                # Empty payload for actions like /off; JSON for others.
                if payload == "" or payload is None:
                    r = requests.post(url, timeout=5)
                else:
                    r = requests.post(url, json=payload, timeout=5)
            elif method == "delete":
                r = requests.delete(url, timeout=5)
            else:
                print(f"    [HTTP] unknown method: {method}")
                return False

            if r.ok:
                return True
            print(f"    [HTTP] {method.upper()} {url} -> {r.status_code}")
            return False
        except requests.exceptions.RequestException as e:
            print(f"    [HTTP] {method.upper()} {url} -> unreachable "
                  f"({type(e).__name__})")
            return False

    def _device_off(self, ip: str) -> bool:
        """Globally turn the device OFF via POST /off."""
        return self._http_send(ip, "/off", payload="", method="post")

    def _device_on(self, ip: str) -> bool:
        """Globally turn the device back ON via DELETE /off."""
        return self._http_send(ip, "/off", method="delete")

    # -------------------------------------------------------------------------
    # RSRUN: per-pump schedule snapshot/override/restore
    # -------------------------------------------------------------------------

    def _rsrun_snapshot(self, ctrl: dict) -> Optional[Dict[str, Any]]:
        """
        Capture the current pump_X subtree from /pump/settings so we can
        restore it as-is on power return.
        """
        ip = ctrl["ip"]
        pump_index = ctrl["pump_index"]  # "pump_1" / "pump_2"
        settings = self._http_get(ip, "/pump/settings")
        if not isinstance(settings, dict):
            return None
        pump_data = settings.get(pump_index)
        if not isinstance(pump_data, dict):
            return None
        # Keep only what's needed for restore. The schedule is the main thing,
        # plus the on/off flag.
        return {
            "type": "rsrun",
            "ip": ip,
            "pump_index": pump_index,
            "schedule": pump_data.get("schedule"),
            "schedule_enabled": pump_data.get("schedule_enabled", True),
        }

    def _rsrun_apply_intensity(self, ctrl: dict, intensity: int) -> bool:
        """
        Push a 1-slot schedule at the requested intensity for one pump of
        an RSRUN. Format: [{"st":0,"pd":0,"ti":<intensity>}].
        """
        ip = ctrl["ip"]
        pump_index = ctrl["pump_index"]
        payload = {
            pump_index: {
                "schedule_enabled": True,
                "schedule": [{"st": 0, "pd": 0, "ti": intensity}],
            }
        }
        return self._http_send(ip, "/pump/settings", payload, "put")

    def _rsrun_restore(self, ctrl: dict, snapshot: Dict[str, Any]) -> bool:
        """Push the saved schedule back to the device."""
        ip = ctrl["ip"]
        pump_index = ctrl["pump_index"]
        if snapshot.get("schedule") is None:
            print(f"    [SNAP] {self._ctrl_label(ctrl)}: no schedule "
                  "in snapshot, skipping restore")
            return False
        payload = {
            pump_index: {
                "schedule_enabled": bool(snapshot.get("schedule_enabled", True)),
                "schedule": snapshot["schedule"],
            }
        }
        return self._http_send(ip, "/pump/settings", payload, "put")

    # -------------------------------------------------------------------------
    # RSWAVE: /auto snapshot/override/restore
    # -------------------------------------------------------------------------

    def _rswave_snapshot(self, ctrl: dict) -> Optional[Dict[str, Any]]:
        """Capture the full /auto payload (intervals + schedule metadata)."""
        ip = ctrl["ip"]
        auto = self._http_get(ip, "/auto")
        if not isinstance(auto, dict) or "intervals" not in auto:
            return None
        return {"type": "rswave", "ip": ip, "auto": auto}

    def _rswave_apply_intensity(self, ctrl: dict, intensity: int) -> bool:
        """
        Push a single uniform-flow interval at the requested intensity.

        Wave type "un" (Uniforme) gives a steady, non-pulsed forward flow
        at `fti`%. The other knobs (rti / frt / rrt / pd / sn) are not
        meaningful for a uniform wave but we set sane defaults to avoid
        firmware complaints.

        Push sequence required by the device:
          POST /auto/init      (with a fresh op uid)
          POST /auto           (the new schedule body, no uid)
          POST /auto/complete  (same uid as init)
          POST /auto/apply     (same uid)
        """
        import uuid
        ip = ctrl["ip"]
        op_uid = str(uuid.uuid4())

        # Build a minimal one-interval uniform schedule covering the whole day.
        new_interval = {
            "wave_uid": op_uid,
            "type": "un",        # uniform: steady continuous flow
            "direction": "fw",
            "frt": 0,            # not meaningful for uniform, but the
            "rrt": 0,            # firmware may require the keys present
            "fti": intensity,    # forward target intensity (the only one
                                 # that actually matters for "un")
            "rti": 0,
            "pd": 0,             # no pulsation
            "sn": True,
            "sync": True,
            "st": 0,             # starts at 00:00
            "start": 0,
        }
        body = {"intervals": [new_interval]}

        if not self._http_send(ip, "/auto/init", {"uid": op_uid}, "post"):
            return False
        if not self._http_send(ip, "/auto", body, "post"):
            return False
        if not self._http_send(ip, "/auto/complete", {"uid": op_uid}, "post"):
            return False
        if not self._http_send(ip, "/auto/apply", {"uid": op_uid}, "post"):
            return False
        return True

    def _rswave_restore(self, ctrl: dict, snapshot: Dict[str, Any]) -> bool:
        """Push the saved /auto payload back to the device."""
        import uuid
        ip = ctrl["ip"]
        auto = snapshot.get("auto")
        if not isinstance(auto, dict):
            print(f"    [SNAP] {self._ctrl_label(ctrl)}: invalid snapshot")
            return False

        op_uid = str(uuid.uuid4())
        body = dict(auto)
        body.pop("uid", None)  # uid is owned by the init/complete/apply cycle

        if not self._http_send(ip, "/auto/init", {"uid": op_uid}, "post"):
            return False
        if not self._http_send(ip, "/auto", body, "post"):
            return False
        if not self._http_send(ip, "/auto/complete", {"uid": op_uid}, "post"):
            return False
        if not self._http_send(ip, "/auto/apply", {"uid": op_uid}, "post"):
            return False
        return True

    # -------------------------------------------------------------------------
    # Snapshot orchestration (capture once before first override, restore once)
    # -------------------------------------------------------------------------

    def _ensure_snapshot(self, ctrl: dict) -> None:
        """
        Capture the device's original configuration the first time we are
        about to override it during an outage. Idempotent: if a snapshot
        already exists on disk, don't overwrite it (we'd lose the original).
        """
        key = ctrl["key"]
        if self._load_snapshot(key) is not None:
            return  # already have one (e.g. survived a Pi reboot)

        hw = ctrl["hw_model"]
        if hw.startswith("RSRUN"):
            snap = self._rsrun_snapshot(ctrl)
        elif hw.startswith("RSWAVE"):
            snap = self._rswave_snapshot(ctrl)
        else:
            return  # unknown family

        if snap is None:
            print(f"    [SNAP] {self._ctrl_label(ctrl)}: snapshot failed")
            return

        # Remember whether the device was ON or OFF at snapshot time.
        # If it was already off (e.g. user toggle), we don't want to
        # turn it back on at restore.
        snap["was_off"] = self._is_device_off(ctrl["ip"])
        self._save_snapshot(key, snap)
        print(f"    [SNAP] {self._ctrl_label(ctrl)}: original config saved")

    def _is_device_off(self, ip: str) -> bool:
        """True iff /mode reports 'off'."""
        mode = self._http_get(ip, "/mode")
        if isinstance(mode, dict):
            return mode.get("mode") == "off"
        return False

    def _api_set(self, ctrl: dict, intensity: int):
        """
        Apply a target intensity to a single pump.

        Strategy:
          - intensity == 0: snapshot once, then POST /off on the device.
            (For multi-pump RSRUN, /off stops the whole box; we only call
             it when ALL its pumps are at 0 — see apply_level dispatch.)
          - intensity > 0: snapshot once, then push a 1-slot schedule
            (RSRUN) or a 1-interval continuous wave (RSWAVE).
        """
        if not REQUESTS_AVAILABLE:
            return
        ip = ctrl.get("ip")
        if not ip:
            return

        label = self._ctrl_label(ctrl)
        hw = ctrl["hw_model"]

        # Always grab a snapshot before our first override
        self._ensure_snapshot(ctrl)

        ok = False
        if intensity == 0:
            # OFF semantics differ between single- and multi-pump devices.
            # Single pump -> just POST /off.
            # Multi-pump RSRUN -> we can't disable the whole box if the
            # OTHER pump still needs to run. Use schedule_enabled=false
            # on this pump only.
            if ctrl.get("pump_index"):
                payload = {ctrl["pump_index"]: {"schedule_enabled": False}}
                ok = self._http_send(ip, "/pump/settings", payload, "put")
            else:
                ok = self._device_off(ip)
        else:
            if hw.startswith("RSRUN"):
                ok = self._rsrun_apply_intensity(ctrl, intensity)
            elif hw.startswith("RSWAVE"):
                ok = self._rswave_apply_intensity(ctrl, intensity)
            else:
                print(f"    [API] {label}: unsupported hw_model {hw}")
                return

        status = "OK" if ok else "FAIL"
        print(f"    [API] {label}@{ip} -> {intensity}% {status}")

    def _api_restore(self, ctrl: dict) -> None:
        """
        Restore the device's original configuration from its snapshot,
        and ensure it is turned back on (unless it was already off).
        """
        if not REQUESTS_AVAILABLE:
            return
        ip = ctrl["ip"]
        label = self._ctrl_label(ctrl)
        snap = self._load_snapshot(ctrl["key"])
        if snap is None:
            print(f"    [SNAP] {label}: no snapshot, nothing to restore")
            return

        hw = ctrl["hw_model"]
        if hw.startswith("RSRUN"):
            ok = self._rsrun_restore(ctrl, snap)
        elif hw.startswith("RSWAVE"):
            ok = self._rswave_restore(ctrl, snap)
        else:
            ok = False

        # If the device had been globally turned off during the outage
        # (single-pump 0% case), bring it back on -- unless it was ALREADY
        # off when we took the snapshot.
        if not snap.get("was_off", False):
            if self._is_device_off(ip):
                self._device_on(ip)

        status = "OK" if ok else "FAIL"
        print(f"    [API] {label}@{ip} restored {status}")

        if ok:
            self._drop_snapshot(ctrl["key"])

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
