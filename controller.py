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
from notifier import Notifier

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

        # Background restore-retry: when mains returns, the Red Sea devices
        # may still be (re)joining Wi-Fi and unreachable for a while. We
        # retry restoring their original config until every snapshot is
        # successfully re-applied (and dropped), instead of giving up after
        # one failed attempt.
        self._restore_retry_thread: Optional[threading.Thread] = None
        self._stop_restore_retry = threading.Event()
        self._restore_cfg = self._pump_cfg.get("restore_retry", {})

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

        At mains return the Red Sea devices may still be rejoining Wi-Fi,
        so a single attempt often fails. We do one immediate pass, then
        spawn a background thread that keeps retrying any pump whose
        snapshot is still on disk (i.e. not yet successfully restored).
        """
        normal = self._resolver.normal_level
        with self._lock:
            self.active_level_name = "normal"
            # Mark all pumps as logically back to normal for status/MQTT.
            for ctrl in self._pump_cfg.get("controllers", []):
                self._device_intensities[ctrl["key"]] = normal.get_intensity(
                    ctrl["key"])
            self._publish_pump_state(normal, "power_restored")

        # One immediate attempt, then background retries for whatever failed.
        remaining = self._restore_pass()
        if remaining:
            self._start_restore_retry()

    def _restore_pass(self) -> int:
        """
        Attempt to restore every pump that still has a snapshot on disk.

        Returns the number of pumps still NOT restored after this pass
        (i.e. snapshots still present). _api_restore drops the snapshot
        only on success, so a lingering snapshot means "retry needed".
        """
        controllers = self._pump_cfg.get("controllers", [])
        remaining = 0
        for ctrl in controllers:
            key = ctrl["key"]
            if self._load_snapshot(key) is None:
                continue  # never overridden, or already restored
            label = self._ctrl_label(ctrl)
            print(f"  [PUMP] {label}: restoring original config")
            self._api_restore(ctrl)
            # If the snapshot is still there, the restore failed.
            if self._load_snapshot(key) is not None:
                remaining += 1
        return remaining

    def _start_restore_retry(self) -> None:
        """Spawn (or restart) the background restore-retry thread."""
        if (self._restore_retry_thread is not None
                and self._restore_retry_thread.is_alive()):
            return  # already retrying
        self._stop_restore_retry.clear()
        self._restore_retry_thread = threading.Thread(
            target=self._restore_retry_loop, daemon=True
        )
        self._restore_retry_thread.start()

    def _restore_retry_loop(self) -> None:
        """
        Keep retrying restore until all snapshots are gone, a stop is
        requested (e.g. a new outage), or we exhaust max attempts.
        """
        interval = float(self._restore_cfg.get("interval_s", 30.0))
        max_attempts = int(self._restore_cfg.get("max_attempts", 40))
        attempt = 0
        print(f"[RESTORE] Background retry started "
              f"(every {interval:.0f}s, up to {max_attempts} attempts)")
        while not self._stop_restore_retry.wait(timeout=interval):
            attempt += 1
            remaining = self._restore_pass()
            if remaining == 0:
                print(f"[RESTORE] All pumps restored after {attempt} retry(ies)")
                return
            if attempt >= max_attempts:
                print(f"[RESTORE] Giving up after {attempt} attempts; "
                      f"{remaining} pump(s) still unrestored. Snapshots kept "
                      f"on disk -- run 'python3 restore_pumps.py' manually.")
                return
            print(f"[RESTORE] Attempt {attempt}: {remaining} pump(s) still "
                  "unreachable, will retry")

    def stop_restore_retry(self) -> None:
        """Cancel any in-flight restore-retry (e.g. a new outage began)."""
        self._stop_restore_retry.set()

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
    def _snapshot_base(self) -> Path:
        """Base directory for all on-disk snapshots."""
        # NOTE: the override lives under pump_control (self._pump_cfg), not
        # at the config root. Reading it from self._cfg was a bug: the
        # override never took effect.
        path = self._pump_cfg.get("snapshot_dir")
        if path:
            return Path(path)
        return Path("/var/lib/reefbeat-energy-backup")

    @property
    def _snapshot_dir(self) -> Path:
        """
        Where pre-outage snapshots live.

        These are captured the first time we override a pump during an
        outage, and deleted once the original config is successfully
        restored. A file here means "restore still pending".
        """
        return self._snapshot_base / "snapshots"

    @property
    def _reference_dir(self) -> Path:
        """
        Where periodic *reference* snapshots live.

        Captured on a timer while running in nominal mode (mains, full
        speed), these are a safety net: they are NOT deleted on restore,
        so we always have a recent known-good config to fall back on even
        if the pre-outage snapshot was never taken or got lost.
        """
        return self._snapshot_base / "reference"

    def _snapshot_path(self, key: str) -> Path:
        """Return the pre-outage snapshot file path for a given pump key."""
        # Sanitise the key for filesystem usage (":" is fine on ext4 but ugly)
        safe = key.replace("/", "_").replace(":", "-")
        return self._snapshot_dir / f"{safe}.json"

    def _reference_path(self, key: str) -> Path:
        """Return the reference snapshot file path for a given pump key."""
        safe = key.replace("/", "_").replace(":", "-")
        return self._reference_dir / f"{safe}.json"

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
        """Remove a pre-outage snapshot file (after successful restore)."""
        try:
            self._snapshot_path(key).unlink(missing_ok=True)
        except OSError:
            pass

    def _save_reference(self, key: str, snapshot: Dict[str, Any]) -> None:
        """Persist a reference snapshot atomically (tmp + rename)."""
        try:
            self._reference_dir.mkdir(parents=True, exist_ok=True)
            path = self._reference_path(key)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshot, indent=2))
            os.replace(tmp, path)
        except OSError as e:
            print(f"    [REFERENCE] failed to save {key}: {e}")

    def _load_reference(self, key: str) -> Optional[Dict[str, Any]]:
        """Load a reference snapshot from disk if present."""
        path = self._reference_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"    [REFERENCE] failed to load {key}: {e}")
            return None

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
        at `fti`%. The firmware requires every interval to have a "name"
        field and specific numeric defaults for frt/rrt/sn.

        Push sequence required by the device:
          POST /auto/init      (with a fresh op uid)
          POST /auto           (the new schedule body, no uid)
          POST /auto/complete  (same uid as init)
          POST /auto/apply     (same uid)
        """
        import uuid
        ip = ctrl["ip"]
        op_uid = str(uuid.uuid4())
        wave_uid = str(uuid.uuid4())

        # Build a minimal one-interval uniform schedule covering the whole day.
        new_interval = {
            "wave_uid": wave_uid,
            "name": "Backup Mode",
            "type": "un",        # uniform: steady continuous flow
            "direction": "fw",
            "frt": 2,            # min 2, max 60
            "rrt": 2,            # min 2, max 60
            "fti": intensity,    # forward target intensity (min 10, max 100)
            "rti": intensity,    # reverse target intensity (min 10, max 100)
            "pd": 2,             # pulse duration (min 2, max 25)
            "sn": 3,             # sine (min 3, max 10)
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

    def _capture_config(self, ctrl: dict) -> Optional[Dict[str, Any]]:
        """
        Read a device's current configuration into a snapshot dict.
        Returns None if the device family is unknown or the read failed.
        """
        hw = ctrl["hw_model"]
        if hw.startswith("RSRUN"):
            return self._rsrun_snapshot(ctrl)
        if hw.startswith("RSWAVE"):
            return self._rswave_snapshot(ctrl)
        return None

    def _ensure_snapshot(self, ctrl: dict) -> None:
        """
        Capture the device's original configuration the first time we are
        about to override it during an outage. Idempotent: if a snapshot
        already exists on disk, don't overwrite it (we'd lose the original).

        If the live read fails (device already unreachable when the outage
        hits), we fall back to the most recent *reference* snapshot so we
        still have something to restore later.
        """
        key = ctrl["key"]
        if self._load_snapshot(key) is not None:
            return  # already have one (e.g. survived a Pi reboot)

        snap = self._capture_config(ctrl)

        if snap is None:
            # Live capture failed — fall back to the periodic reference.
            ref = self._load_reference(key)
            if ref is not None:
                self._save_snapshot(key, ref)
                print(f"    [SNAP] {self._ctrl_label(ctrl)}: live capture "
                      "failed, using reference snapshot as fallback")
            else:
                print(f"    [SNAP] {self._ctrl_label(ctrl)}: snapshot failed "
                      "and no reference available")
            return

        # Remember whether the device was ON or OFF at snapshot time.
        # If it was already off (e.g. user toggle), we don't want to
        # turn it back on at restore.
        snap["was_off"] = self._is_device_off(ctrl["ip"])
        self._save_snapshot(key, snap)
        print(f"    [SNAP] {self._ctrl_label(ctrl)}: original config saved")

    def capture_reference_snapshots(self, force: bool = False) -> int:
        """
        Periodically capture each pump's current config as a *reference*
        snapshot (safety net). Should only be called in nominal mode
        (mains power, pumps at normal intensity) so we never capture a
        reduced config as the reference.

        Unlike pre-outage snapshots, references are kept indefinitely and
        overwritten on each successful capture. Returns the number of
        devices captured.

        `force=True` bypasses the nominal-mode guard (used by manual tools).
        """
        if not force and self.active_level_name != "normal":
            # Don't capture while we're running a reduced level: that would
            # poison the reference with the eco/critical config.
            return 0

        captured = 0
        for ctrl in self._pump_cfg.get("controllers", []):
            snap = self._capture_config(ctrl)
            if snap is None:
                continue  # device unreachable this round; keep old reference
            snap["was_off"] = self._is_device_off(ctrl["ip"])
            snap["captured_at"] = time.time()
            self._save_reference(ctrl["key"], snap)
            captured += 1
        if captured:
            print(f"[REFERENCE] Captured {captured} pump config(s) as reference")
        return captured

    def _probe_device(self, ip: str, timeout_s: float = 2.0) -> Optional[str]:
        """
        Lightweight reachability probe for one device. Returns the device's
        reported mode string (e.g. 'auto', 'manual', 'off') if reachable,
        or None if unreachable. Stays quiet on failure (the health_check
        caller does the logging) to avoid spamming per-request HTTP errors.
        """
        if not REQUESTS_AVAILABLE:
            return None
        try:
            r = requests.get(f"http://{ip}/mode", timeout=timeout_s)
            if r.ok:
                data = r.json()
                if isinstance(data, dict):
                    return str(data.get("mode", "on"))
                return "on"
        except Exception:  # noqa: BLE001 — any failure = unreachable
            return None
        return None

    def health_check(self, network_mode: str = "?",
                     on_battery: bool = False) -> Dict[str, bool]:
        """
        Poll every configured device for reachability and log a single
        summary line. Returns a {label: reachable} map.

        This is the periodic "is everyone OK?" check. It also surfaces the
        active network mode (client / rejoin / hotspot) so the log shows
        WHERE we are reaching the devices from — useful to confirm that a
        hotspot failover actually brought the pumps back.
        """
        controllers = self._pump_cfg.get("controllers", [])
        results: Dict[str, bool] = {}
        # Shorter timeout on battery to avoid blocking the loop and wasting
        # energy on slow retries; devices on hotspot answer fast or not at all.
        timeout_s = 1.5 if on_battery else 2.5

        ok_count = 0
        details = []
        seen_ips = {}
        for ctrl in controllers:
            ip = ctrl.get("ip")
            label = self._ctrl_label(ctrl)
            if not ip:
                results[label] = False
                details.append(f"{label}=no-ip")
                continue
            # Cache per-IP probe: multi-pump RSRUN share one IP/controller.
            if ip in seen_ips:
                mode = seen_ips[ip]
            else:
                mode = self._probe_device(ip, timeout_s=timeout_s)
                seen_ips[ip] = mode
            reachable = mode is not None
            results[label] = reachable
            if reachable:
                ok_count += 1
                details.append(f"{label}={mode}")
            else:
                details.append(f"{label}=DOWN")

        total = len(results)
        icon = "✅" if ok_count == total and total > 0 else (
            "⚠️" if ok_count > 0 else "❌")
        ctx = "battery" if on_battery else "mains"
        print(f"[HEALTH] {icon} {ok_count}/{total} devices reachable "
              f"| net={network_mode} | {ctx} | "
              + ", ".join(details))
        return results

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
                 network: NetworkManager, cfg: dict,
                 notifier: "Notifier" = None):
        self._pump = pump
        self._network = network
        self._notifier = notifier
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
            # A new outage supersedes any in-flight restore retry.
            self._pump.stop_restore_retry()
            if self._notifier:
                capacity = self._cfg.get("battery", {}).get("capacity_ah", 60)
                runtime = (self.soc / 100 * capacity) / 4.0 if self.soc > 0 else 0
                self._notifier.notify_outage(self.soc, runtime)
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
            if self._notifier:
                self._notifier.notify_power_restored(duration, self.soc)

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

        # Notify about network failover mode
        if self._notifier and self._network.mode.value != "client":
            self._notifier.notify_network_failover(self._network.mode.value)

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

    def update_soc(self, soc: float, runtime_h: float = -1):
        """Update SoC and adjust pump levels if on battery."""
        old_soc = self.soc
        self.soc = soc
        if self.power_state == PowerState.BATTERY:
            old_level = self._pump.active_level_name
            self._pump.apply_level(soc, on_battery=True)
            new_level = self._pump.active_level_name

            # Notify on level change
            if self._notifier and new_level != old_level and new_level != "normal":
                self._notifier.notify_level_change(new_level, soc, runtime_h)

            # Notify on critical SoC (repeated)
            critical_threshold = (self._cfg.get("pump_control", {})
                                  .get("levels", {})
                                  .get("critical", {})
                                  .get("soc_threshold", 15))
            if self._notifier and soc <= critical_threshold:
                self._notifier.notify_soc_critical(soc, runtime_h)

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
