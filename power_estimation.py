"""
Power consumption estimation and outage scenario generation.

This module provides three independent helpers used by the wizard to
build smart, autonomy-targeted backup scenarios:

  1. Per-device power tables for the equipment we manage (ReefWave
     wavemakers, ReefRun return/skimmer pumps).
  2. Raspberry Pi auto-detection with model-specific idle power figures.
  3. A scenario builder that takes a target autonomy in hours, the
     battery capacity in Wh, and the list of equipment, and returns a
     list of degradation levels (SoC threshold + per-device intensity)
     that should hit the target.

The intent is that the wizard reads the target ("I want 12 h of
autonomy on a 1.5 kWh battery") and proposes a plausible degradation
plan instead of asking the user to guess SoC thresholds.

All wattages here are approximations based on manufacturer data sheets
or product comparisons; they are good enough for the scenario maths
which itself targets a 70-80% confidence level rather than physical
exactness.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# =============================================================================
# Per-device power tables
# =============================================================================
# Each entry maps a "model" identifier (the same string shape as what
# Red Sea's /dashboard endpoint reports in pump_model, e.g. "return-12000"
# or "rsk-900") to a tuple (P_min_W, P_max_W). P_min_W is the consumption
# at the lowest running intensity (which firmware-side is around 40% for
# ReefRun, 10% for ReefWave); P_max_W is the consumption at 100%.

# ReefWave wavemakers (single hw_model exposed on the controller, but
# the actual model sits in pump_model). Power figures from Red Sea
# datasheets.
REEFWAVE_POWER: Dict[str, Tuple[float, float]] = {
    # Catalogue model -> (Wmin, Wmax)
    "ReefWave 25": (4.0, 25.0),
    "ReefWave 45": (8.0, 45.0),
}

# ReefRun DC pumps (G1 and G2). Estimated from the hydraulic power
# divided by a typical DC pump efficiency of 55-60%, then sanity-checked
# against the 24V nominal. Conservative -- real consumption tends to be
# slightly lower in the storage / low-flow regimes.
REEFRUN_POWER: Dict[str, Tuple[float, float]] = {
    # Return pumps
    "return-4000":   (10.0, 30.0),   # G2 4000
    "return-5500":   (12.0, 40.0),   # G1 5500
    "return-6000":   (12.0, 45.0),   # G2 6000
    "return-7000":   (15.0, 55.0),   # G1 7000
    "return-8000":   (15.0, 60.0),   # G2 8000
    "return-9000":   (18.0, 70.0),   # G1 9000
    "return-12000":  (20.0, 90.0),   # G2 12000
    # Skimmer pumps (Red Sea DC Skimmers)
    "rsk-300":       (4.0, 12.0),
    "rsk-600":       (5.0, 20.0),
    "rsk-900":       (6.0, 28.0),
    "rsk-1200":      (8.0, 38.0),
}


def device_power_at(pump_model: str, intensity_pct: int) -> float:
    """
    Linear interpolation between Pmin (at the firmware floor) and Pmax
    (at 100%). Below the floor the firmware refuses to run, so 0% means
    OFF (= 0 W). Above the floor we do a linear blend, which slightly
    overestimates at low intensity (real curves are quadratic-ish) but
    is conservative for autonomy planning.
    """
    if intensity_pct <= 0:
        return 0.0

    # Pick the right table; pump_model strings come from /dashboard.
    table: Optional[Dict[str, Tuple[float, float]]]
    key = pump_model
    if pump_model.startswith("return-") or pump_model.startswith("rsk-"):
        table = REEFRUN_POWER
    elif pump_model.startswith("ReefWave"):
        table = REEFWAVE_POWER
    else:
        table = None

    if table is None or key not in table:
        # Unknown model: fall back to a sane mid-range default so the
        # scenario maths still produce something rather than crashing.
        return _generic_estimate(intensity_pct)

    p_min, p_max = table[key]
    # The firmware floor is the lowest non-zero intensity allowed.
    # We use 40% for ReefRun, 10% for ReefWave; below that, the device
    # simply doesn't run, but we may be asked about an in-range value
    # below it (clamped upstream). Be defensive.
    floor = 40 if table is REEFRUN_POWER else 10
    if intensity_pct < floor:
        return p_min  # treat as if running at the floor

    # Linear blend between floor and 100%
    ratio = (intensity_pct - floor) / (100 - floor)
    ratio = max(0.0, min(1.0, ratio))
    return p_min + (p_max - p_min) * ratio


def _generic_estimate(intensity_pct: int) -> float:
    """Crude fallback for unknown models: 25 W max at 100%, scaled."""
    return 0.25 * intensity_pct


# =============================================================================
# Raspberry Pi auto-detection
# =============================================================================
# Idle power figures are typical for headless operation with an attached
# I2C peripheral and a Wi-Fi link. Real-world numbers can vary by ~30%.

_PI_POWER: Dict[str, float] = {
    "Pi 3":         3.5,   # B/B+
    "Pi 4":         4.0,   # 2/4/8 GB, idle headless
    "Pi 5":         5.5,
    "Pi Zero":      1.5,
    "Pi Zero 2":    2.0,
    "Pi 400":       4.0,
    "Compute":      4.5,   # CM4
}


def detect_raspberry_pi() -> Tuple[Optional[str], float]:
    """
    Read /proc/device-tree/model (or fall back to /proc/cpuinfo) and
    return (model_label, estimated_watts).

    Returns (None, 0.0) if we can't tell -- the caller should then ask
    the user to provide a value manually.
    """
    candidate = ""

    # /proc/device-tree/model is the canonical source on Raspberry Pi.
    # It contains a NUL-terminated string like "Raspberry Pi 4 Model B".
    try:
        with open("/proc/device-tree/model", "rb") as f:
            candidate = f.read().decode("ascii", errors="ignore").strip("\x00 ")
    except OSError:
        pass

    if not candidate:
        # Fall back to /proc/cpuinfo's "Model:" line on older kernels.
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.lower().startswith("model"):
                        candidate = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass

    if not candidate:
        return (None, 0.0)

    # Match the most specific tag first: "Pi Zero 2" before "Pi Zero",
    # "Pi 5" before "Pi", etc.
    for tag in ("Pi Zero 2", "Pi 400", "Compute", "Pi 5", "Pi 4",
                "Pi 3", "Pi Zero"):
        if tag in candidate:
            return (candidate, _PI_POWER[tag])

    # Unknown Pi variant -- return label without a watt estimate so the
    # wizard knows it has to ask.
    return (candidate, 0.0)


# =============================================================================
# Scenario builder
# =============================================================================

@dataclass
class DeviceSpec:
    """Minimal spec the scenario builder needs about a controlled pump.

    `key` is the unique pump key (matches what PumpController uses).
    `family` is "wave" or "run" to drive the floor / shedding policy.
    `pump_model` is used to look up power figures.
    `role` is "return", "skimmer", "wave" -- the policy for shedding
    differs by role (skimmer is the first to drop, return-pump survives
    the longest).
    """
    key: str
    family: str          # "wave" or "run"
    role: str            # "wave" / "return" / "skimmer"
    pump_model: str
    floor_pct: int       # firmware-allowed minimum running intensity


@dataclass
class ScenarioLevel:
    """One entry in the resulting backup plan."""
    name: str
    soc_threshold: int                # SoC below which this level kicks in
    per_device: Dict[str, int]        # key -> intensity %
    duration_h: float                 # how long this level is expected to last
    avg_power_w: float                # average draw at this level
    wh_budget: float                  # Wh consumed during this level


def build_scenario(
    target_h: float,
    capacity_wh: float,
    devices: List[DeviceSpec],
    aux_load_w: float = 0.0,
    num_levels: Optional[int] = None,
) -> List[ScenarioLevel]:
    """
    Generate a degradation plan that aims to last ``target_h`` hours.

    Strategy:
      - "normal": full speed, no SoC trigger (this is the always-on level).
      - Then 1 to 4 degraded levels, each shedding more equipment:
          1. Skimmer to 0%, return + wave to ~70%
          2. Skimmer 0%, return at floor, waves at ~50%
          3. Everything off except waves at floor
      - SoC thresholds are spaced so each level lasts ~target_h / N hours
        given its own average power.

    Returns levels in *order of activation* (high SoC threshold first).
    The "normal" level is included as the first entry with threshold
    100 -- this matches what PumpController expects.

    Parameters
    ----------
    target_h     : autonomy goal in hours. Drives how aggressive the
                   degradation curve is.
    capacity_wh  : usable battery energy in Wh (Ah * voltage * DoD).
    devices      : list of DeviceSpec, one per pump.
    aux_load_w   : fixed extra load (Pi + I2C + Victron + ...) in W.
    num_levels   : 1..4, number of degraded levels. Auto-picked from
                   target_h if None: short outages get fewer levels.
    """
    if num_levels is None:
        # Short targets => fewer steps; long targets => more steps.
        if target_h <= 4:
            num_levels = 1
        elif target_h <= 8:
            num_levels = 2
        elif target_h <= 16:
            num_levels = 3
        else:
            num_levels = 4
    num_levels = max(1, min(4, num_levels))

    # Always include the "normal" reference level.
    levels: List[ScenarioLevel] = [
        ScenarioLevel(
            name="normal",
            soc_threshold=100,
            per_device={d.key: 100 for d in devices},
            duration_h=0.0,
            avg_power_w=_compute_power(devices, {d.key: 100 for d in devices})
                        + aux_load_w,
            wh_budget=0.0,
        )
    ]

    # Per-level intensity policy: progressively shed equipment.
    # Index 0 = first degraded level (least aggressive),
    # higher indices = more aggressive.
    policies = _degradation_policies(num_levels)

    # Compute the wh budget for each degraded level. We split the
    # capacity proportionally so that each level lasts about the same
    # fraction of target_h (after weighting by its own power).
    remaining_wh = capacity_wh
    per_level_target_h = target_h / num_levels

    # SoC threshold spacing: from 80% downwards, equally divided.
    # E.g. 4 levels => thresholds at 80, 60, 40, 20.
    soc_top, soc_bottom = 80, 20
    if num_levels == 1:
        thresholds = [50]
    else:
        step = (soc_top - soc_bottom) / (num_levels - 1)
        thresholds = [int(soc_top - step * i) for i in range(num_levels)]

    level_names = ["eco", "survival", "critical", "minimum"]
    for i, policy in enumerate(policies):
        per_device = {d.key: policy(d) for d in devices}
        avg_p = _compute_power(devices, per_device) + aux_load_w
        # Wh budget for this level: aim for per_level_target_h hours,
        # capped at the remaining capacity.
        budget = min(remaining_wh, avg_p * per_level_target_h)
        duration = budget / avg_p if avg_p > 0 else 0.0
        levels.append(ScenarioLevel(
            name=level_names[i],
            soc_threshold=thresholds[i],
            per_device=per_device,
            duration_h=round(duration, 1),
            avg_power_w=round(avg_p, 1),
            wh_budget=round(budget, 1),
        ))
        remaining_wh -= budget

    return levels


def _compute_power(devices: List[DeviceSpec],
                   per_device: Dict[str, int]) -> float:
    """Sum the power consumption of all devices at the given intensities."""
    total = 0.0
    for d in devices:
        intensity = per_device.get(d.key, 0)
        total += device_power_at(d.pump_model, intensity)
    return total


def _degradation_policies(n: int) -> List:
    """
    Return a list of n policy functions, each taking a DeviceSpec and
    returning the target intensity (%) for that device at that level.
    Levels go from least aggressive (level 1) to most aggressive (level n).
    """
    # Level recipes, ordered from gentlest to harshest. The build_scenario
    # caller will use num_levels of these starting from index 0.
    recipes = [
        # 1: trim everyone to a comfortable mid-range
        lambda d: (
            70 if d.role == "wave"
            else 70 if d.role == "return"
            else 60   # skimmer kept on but reduced
        ),
        # 2: shed skimmer, keep return at firmware floor, waves at ~50%
        lambda d: (
            50 if d.role == "wave"
            else d.floor_pct if d.role == "return"
            else 0
        ),
        # 3: keep only waves at floor (fish welfare minimum: water motion)
        lambda d: (
            d.floor_pct if d.role == "wave"
            else 0
        ),
        # 4: emergency, everything off
        lambda d: 0,
    ]
    return recipes[:n]


# =============================================================================
# Pretty-print helper for the wizard
# =============================================================================

def format_scenario(levels: List[ScenarioLevel],
                    target_h: float,
                    capacity_wh: float) -> str:
    """Build a human-readable summary of the scenario, in French."""
    lines = []
    total_h = sum(lvl.duration_h for lvl in levels if lvl.name != "normal")
    lines.append(
        f"Plan d'autonomie : cible {target_h:.0f} h, "
        f"batterie {capacity_wh:.0f} Wh, "
        f"estimé {total_h:.1f} h"
    )
    lines.append("")
    for lvl in levels:
        if lvl.name == "normal":
            lines.append(f"  • {lvl.name:10s} (>{lvl.soc_threshold:3d}% SoC)  "
                         f"{lvl.avg_power_w:5.1f} W  référence")
            continue
        lines.append(f"  • {lvl.name:10s} (<{lvl.soc_threshold:3d}% SoC)  "
                     f"{lvl.avg_power_w:5.1f} W  ~{lvl.duration_h:4.1f} h")
        for key, intensity in lvl.per_device.items():
            label = "OFF" if intensity == 0 else f"{intensity}%"
            lines.append(f"      - {key:30s} {label}")
    return "\n".join(lines)
