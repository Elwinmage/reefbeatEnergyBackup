"""
reefbeat⚡Backup — Cumulative energy meters for the Home Assistant
Energy dashboard.

Why a dedicated accumulator?
  The INA226 (and the Victron auxiliary, when present) measure
  *instantaneous* voltage and current. The HA Energy dashboard, on the
  other hand, expects cumulative kWh counters with
  ``device_class: energy`` and ``state_class: total_increasing``. This
  module integrates power over time at each main-loop tick to produce
  three such counters:

      - ``energy_discharged_kwh``: energy flowing OUT of the battery
        (battery → load). Feeds the "Energy coming out of battery" slot
        in the Energy dashboard's storage section.
      - ``energy_charged_kwh``: energy flowing INTO the battery (mains
        charger → battery). Feeds the "Energy going in to battery" slot.
      - ``energy_consumed_kwh``: total system load (pumps + RPi)
        regardless of source. On battery this equals the discharge; on
        mains it is derived from the Victron charger output minus what
        goes into the battery (charger_power + battery_power, since
        battery_power is negative while charging).

Persistence:
  Counters are saved to a JSON file (atomic write) at a configurable
  cadence so that a reboot does not reset the totals. With
  ``state_class: total_increasing`` HA tolerates a drop (treating it as
  a counter reset) so a worst-case unclean shutdown only loses up to
  ``save_interval_s`` seconds of accumulation — never produces a
  negative spike.

Sign convention (from ``monitor.MonitorReading``):
  * ``battery_power > 0`` → discharging (energy leaving the battery)
  * ``battery_power < 0`` → charging   (energy entering the battery)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


class EnergyAccumulator:
    """Integrates power over time into persistent kWh counters."""

    # Drop integration steps longer than this. A real main-loop tick is a
    # few seconds; anything past this threshold means the process was
    # paused (debugger, SIGSTOP, long BLE timeout, sleep) and integrating
    # the previous power over that gap would create an artificial spike.
    _MAX_DT_S = 60.0

    def __init__(self, state_path: Path,
                 save_interval_s: float = 60.0) -> None:
        self._state_path = Path(state_path)
        self._save_interval_s = float(save_interval_s)

        self.energy_discharged_kwh: float = 0.0
        self.energy_charged_kwh: float = 0.0
        self.energy_consumed_kwh: float = 0.0

        self._last_t: Optional[float] = None
        self._last_save_t: float = 0.0
        self._dirty = False

        self._load()

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _load(self) -> None:
        """Restore counters from disk; start fresh if absent or corrupt."""
        try:
            data = json.loads(self._state_path.read_text())
            self.energy_discharged_kwh = float(data.get("discharged_kwh", 0.0))
            self.energy_charged_kwh = float(data.get("charged_kwh", 0.0))
            self.energy_consumed_kwh = float(data.get("consumed_kwh", 0.0))
            print(f"[ENERGY] Restored counters from {self._state_path}: "
                  f"discharged={self.energy_discharged_kwh:.3f} kWh, "
                  f"charged={self.energy_charged_kwh:.3f} kWh, "
                  f"consumed={self.energy_consumed_kwh:.3f} kWh")
        except FileNotFoundError:
            print(f"[ENERGY] No previous state at {self._state_path}, "
                  "starting from zero")
        except (ValueError, OSError, KeyError) as e:
            # Corrupt or unreadable file — start fresh rather than crash.
            # HA will see a reset (drop to 0) and start a new period.
            print(f"[ENERGY] Could not read {self._state_path} ({e}), "
                  "starting from zero")

    def _save(self) -> None:
        """Persist counters atomically (tmp + rename) to survive crashes."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(
                self._state_path.suffix + ".tmp")
            payload = {
                "discharged_kwh": round(self.energy_discharged_kwh, 6),
                "charged_kwh": round(self.energy_charged_kwh, 6),
                "consumed_kwh": round(self.energy_consumed_kwh, 6),
                "saved_at": time.time(),
            }
            tmp.write_text(json.dumps(payload, indent=2))
            os.replace(tmp, self._state_path)
            self._dirty = False
        except OSError as e:
            print(f"[ENERGY] Save failed: {e}")

    def flush(self) -> None:
        """Force a save now (used on graceful shutdown)."""
        if self._dirty:
            self._save()

    # -----------------------------------------------------------------
    # Integration
    # -----------------------------------------------------------------

    def update(self, battery_power_w: float,
               charger_power_w: Optional[float],
               on_mains: bool) -> None:
        """Accumulate one main-loop tick worth of energy.

        Parameters
        ----------
        battery_power_w
            Instantaneous battery power in watts.
            Positive = discharging, negative = charging.
        charger_power_w
            Instantaneous charger output power in watts (Victron). Pass
            None when no charger telemetry is available — the
            ``consumed`` counter will then only advance while on battery.
        on_mains
            True when running on grid power, False during an outage.
        """
        now = time.monotonic()

        # First call: just anchor the clock; no integration yet.
        if self._last_t is None:
            self._last_t = now
            self._last_save_t = now
            return

        dt_s = now - self._last_t
        self._last_t = now

        # Drop pathological gaps. dt_s can be negative if monotonic is
        # somehow rewound; in any case bail out for that tick.
        if dt_s <= 0 or dt_s > self._MAX_DT_S:
            return

        dt_h = dt_s / 3600.0

        # --- Battery direction integrators -------------------------------
        # We split charged vs. discharged because the HA Energy dashboard
        # wants two separate cumulative entities for the storage section.
        if battery_power_w > 0:
            # Discharging: kWh = W × h / 1000
            self.energy_discharged_kwh += (battery_power_w * dt_h) / 1000.0
            self._dirty = True
        elif battery_power_w < 0:
            self.energy_charged_kwh += (abs(battery_power_w) * dt_h) / 1000.0
            self._dirty = True

        # --- System load (consumed energy) -------------------------------
        # On battery: load is exactly what the battery is delivering.
        # On mains with a charger: load = charger output minus what is
        # currently flowing INTO the battery. Because charging is
        # represented as negative battery_power, the signed sum
        # `charger_power + battery_power` already gives the right number
        # (charger - (charging current × voltage)).
        # On mains without charger telemetry: the actual load is not
        # observable here, so we skip rather than guess.
        load_w: Optional[float] = None
        if not on_mains:
            if battery_power_w > 0:
                load_w = battery_power_w
        else:
            if charger_power_w is not None and charger_power_w > 0:
                load_w = charger_power_w + battery_power_w
                if load_w < 0:
                    # Charger reading lags battery reading; the algebra
                    # can briefly go negative on transients. Floor at 0
                    # so the cumulative counter never decreases.
                    load_w = 0.0

        if load_w is not None and load_w > 0:
            self.energy_consumed_kwh += (load_w * dt_h) / 1000.0
            self._dirty = True

        # --- Periodic save ----------------------------------------------
        if now - self._last_save_t >= self._save_interval_s:
            self._save()
            self._last_save_t = now

    # -----------------------------------------------------------------
    # Snapshot for telemetry / MQTT
    # -----------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a dict of the current counter values for MQTT publishing.

        Rounded to 3 decimals (Wh resolution) — finer precision is
        meaningless given the INA226 noise floor and avoids cluttering
        the long-term statistics in HA.
        """
        return {
            "energy_discharged_kwh": round(self.energy_discharged_kwh, 3),
            "energy_charged_kwh": round(self.energy_charged_kwh, 3),
            "energy_consumed_kwh": round(self.energy_consumed_kwh, 3),
        }
