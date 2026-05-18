"""
Battery monitoring backends.

Provides a unified interface for reading battery voltage, current,
and computing SoC. Two backends available:
  - INA226: I2C shunt monitor (direct hardware)
  - Victron: BLE connection to Victron Blue Smart IP22 charger
"""

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# SoC estimator (shared by all backends)
# =============================================================================

# LiFePO4 8S (24V nominal) voltage-to-SoC lookup.
#
# LiFePO4 chemistry has a very flat discharge curve in its middle range
# (roughly 20%-90% SoC), so voltage alone is a poor SoC indicator there.
# This table is meant only for coarse calibration at the extremes (full
# pack and near-empty pack) where voltage moves enough to be useful.
#
# Values are rest voltages (no load): the pack reads slightly higher
# during charge and slightly lower during discharge.
_SOC_TABLE = [
    (27.60, 100),  # fully charged at rest
    (27.20,  99),  # top of plateau
    (26.80,  90),
    (26.60,  70),
    (26.40,  50),  # mid-plateau (very flat in reality)
    (26.20,  30),
    (26.00,  20),
    (25.60,  10),
    (24.80,   5),
    (22.40,   0),  # cells fully empty
]


def voltage_to_soc(v: float) -> float:
    """Estimate SoC from pack voltage using linear interpolation.

    For LiFePO4 this is only reliable at the very top and very bottom of
    the curve. The CoulombCounter weighs this estimate very lightly to
    avoid being dragged around by an unreliable signal.
    """
    if v >= _SOC_TABLE[0][0]:
        return 100.0
    if v <= _SOC_TABLE[-1][0]:
        return 0.0
    for i in range(len(_SOC_TABLE) - 1):
        vh, sh = _SOC_TABLE[i]
        vl, sl = _SOC_TABLE[i + 1]
        if vl <= v <= vh:
            return sl + (v - vl) / (vh - vl) * (sh - sl)
    return 50.0


@dataclass
class CoulombCounter:
    """Track SoC by integrating current over time.

    Two operating modes:

    **On battery** (on_mains=False):
      Primary signal is current integrated over time (coulomb counting).
      A light voltage-based correction is applied only at the steep ends
      of the LiFePO4 curve (above 27.20 V or below 26.20 V). On the flat
      plateau the voltage tells us almost nothing, so we trust the
      integrator.

    **On mains** (on_mains=True):
      The charger is active and the INA226 sees a mix of charger current
      and load current with significant noise. Coulomb counting drifts
      because the net current fluctuates around zero. In this mode, we
      rely primarily on **voltage** to estimate SoC:
        - If voltage is in the float range (>27.0 V), battery is full → 100%
        - If voltage is in absorption (26.5-27.0 V), anchor strongly to
          the voltage-based SoC
        - Below that, something is wrong (charger failing?) — we still
          use voltage anchoring with moderate blending
    """
    capacity_ah: float
    soc: float = 100.0
    _last_time: Optional[float] = field(default=None, repr=False)

    def update(self, current: float, voltage: float,
               on_mains: bool = False) -> float:
        now = time.monotonic()

        if on_mains:
            # On mains: trust voltage, not coulomb counting.
            # Charger noise makes current integration unreliable.
            self._last_time = now

            if voltage >= 27.0:
                # Float charge: battery is full
                # Converge quickly to 100% (10% per cycle)
                self.soc = self.soc * 0.90 + 100.0 * 0.10
            elif voltage >= 26.5:
                # Absorption: nearly full, use voltage table
                target = voltage_to_soc(voltage)
                self.soc = self.soc * 0.95 + target * 0.05
            else:
                # Unusual on mains — charger issue? Use voltage estimate
                target = voltage_to_soc(voltage)
                self.soc = self.soc * 0.97 + target * 0.03

            self.soc = max(0.0, min(100.0, self.soc))
            return self.soc

        # On battery: coulomb counting is the primary signal
        if self._last_time is not None:
            dt_h = (now - self._last_time) / 3600.0
            self.soc -= (current * dt_h / self.capacity_ah) * 100.0
            self.soc = max(0.0, min(100.0, self.soc))
        self._last_time = now

        # Voltage-based anchoring: only at steep ends of LFP curve
        if voltage > 1.0 and (voltage > 27.20 or voltage < 26.20):
            target = voltage_to_soc(voltage)
            self.soc = self.soc * 0.995 + target * 0.005
            self.soc = max(0.0, min(100.0, self.soc))
        return self.soc


# =============================================================================
# Battery reading data class
# =============================================================================

@dataclass
class BatteryReading:
    """Unified battery reading from any backend.

    Core fields come from the main battery monitor (INA226 on the bus).
    The optional `charger_*` fields are filled in by an auxiliary monitor
    (e.g. a Victron IP22 charger) when present, so MQTT/HA can surface
    charger state alongside the actual battery state.
    """
    voltage: float = 0.0
    current: float = 0.0       # Positive = discharging, negative = charging
    power: float = 0.0
    soc: float = 100.0
    is_charging: bool = False
    is_discharging: bool = False
    source: str = "unknown"

    # Optional charger telemetry (None when no auxiliary backend is active)
    charger_voltage: Optional[float] = None
    charger_current: Optional[float] = None
    charger_state: Optional[str] = None        # e.g. "storage", "bulk", ...
    charger_error: Optional[str] = None
    charger_source: Optional[str] = None       # e.g. "victron-ip22"


# =============================================================================
# Abstract backend
# =============================================================================

class BatteryMonitorBackend(ABC):
    """Abstract interface for battery monitoring backends."""

    @abstractmethod
    def initialize(self) -> bool:
        """Initialize the backend. Returns True on success."""
        ...

    @abstractmethod
    def read(self, on_mains: bool = False) -> BatteryReading:
        """Read current battery state. on_mains=True when charger is active."""
        ...

    @abstractmethod
    def close(self):
        """Release resources."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend display name."""
        ...


# =============================================================================
# INA226 backend
# =============================================================================

class INA226Backend(BatteryMonitorBackend):
    """Battery monitoring via INA226 I2C shunt monitor."""

    _REG_CONFIG = 0x00
    _REG_SHUNT_VOLTAGE = 0x01
    _REG_BUS_VOLTAGE = 0x02
    _REG_MANUFACTURER_ID = 0xFE

    def __init__(self, cfg: dict, battery_cfg: dict):
        self._bus_num = cfg.get("i2c_bus", 1)
        self._address = int(cfg.get("address", "0x40"), 0)
        self._shunt_ohms = cfg.get("shunt_resistor", 0.01)
        self._bus_v_lsb = 1.25e-3
        self._shunt_v_lsb = 2.5e-6
        self._bus = None
        self._counter = CoulombCounter(
            capacity_ah=battery_cfg.get("capacity_ah", 60.0),
            soc=battery_cfg.get("initial_soc", 100.0),
        )

    @property
    def name(self) -> str:
        return "INA226"

    def initialize(self) -> bool:
        # Pre-flight: is the I2C bus device present at all? Failing here
        # with a clear hint avoids the cryptic "Errno 2 No such file or
        # directory" the smbus2 import would otherwise produce.
        bus_path = f"/dev/i2c-{self._bus_num}"
        if not os.path.exists(bus_path):
            print(f"[INA226] {bus_path} not found -- I2C bus is not active.")
            print("[INA226] Enable it on the Pi with:")
            print("           sudo raspi-config nonint do_i2c 0")
            print("           sudo modprobe i2c-dev")
            print("         then restart the service.")
            return False

        try:
            import smbus2
        except ImportError:
            print("[INA226] python3-smbus2 not installed.")
            print("         Install with: pip3 install --break-system-packages smbus2")
            return False

        try:
            self._bus = smbus2.SMBus(self._bus_num)
            # Configure: continuous shunt+bus, 1.1ms, 16 averages
            self._write_reg(self._REG_CONFIG, 0x4427)
            time.sleep(0.1)
            # Verify manufacturer ID -- 0x5449 ('TI') confirms it's actually
            # the INA226 we're talking to and not some other I2C device
            # that happens to live at this address.
            mid = self._read_reg(self._REG_MANUFACTURER_ID)
            if mid == 0x5449:
                print(f"[INA226] Initialized (bus={self._bus_num}, "
                      f"addr=0x{self._address:02X}, shunt={self._shunt_ohms}Ω)")
                return True
            else:
                print(f"[INA226] WARNING: unexpected manufacturer ID 0x{mid:04X} "
                      "(expected 0x5449 for TI INA226). Wrong device or wrong "
                      "address?")
                return True  # Still try to work in case the chip is a clone
        except OSError as e:
            # Errno 121 (EREMOTEIO): no chip ACK at this address.
            # Errno 5 / 110: bus issue (wiring, pull-ups, voltage).
            print(f"[INA226] I/O error at addr 0x{self._address:02X}: {e}")
            print("[INA226] Check: SDA/SCL wiring, 3.3V power, address jumpers,")
            print("         and 'i2cdetect -y 1' to confirm the chip is visible.")
            return False
        except Exception as e:
            print(f"[INA226] Init failed: {e}")
            return False

    def read(self, on_mains: bool = False) -> BatteryReading:
        voltage = self._read_reg_signed(self._REG_BUS_VOLTAGE) * self._bus_v_lsb
        v_shunt = self._read_reg_signed(self._REG_SHUNT_VOLTAGE) * self._shunt_v_lsb
        current = v_shunt / self._shunt_ohms
        power = voltage * current
        soc = self._counter.update(current, voltage, on_mains=on_mains)

        return BatteryReading(
            voltage=round(voltage, 2),
            current=round(current, 3),
            power=round(power, 1),
            soc=round(soc, 1),
            is_charging=current < -0.05,
            is_discharging=current > 0.05,
            source="ina226",
        )

    def close(self):
        if self._bus:
            self._bus.close()
            print("[INA226] Closed")

    def _write_reg(self, reg: int, val: int):
        self._bus.write_i2c_block_data(
            self._address, reg, [(val >> 8) & 0xFF, val & 0xFF]
        )

    def _read_reg(self, reg: int) -> int:
        d = self._bus.read_i2c_block_data(self._address, reg, 2)
        return (d[0] << 8) | d[1]

    def _read_reg_signed(self, reg: int) -> int:
        raw = self._read_reg(reg)
        return raw - 0x10000 if raw >= 0x8000 else raw


# =============================================================================
# Victron BLE backend
# =============================================================================
# Victron BLE auxiliary monitor (charger telemetry)
# =============================================================================
#
# This is NOT a primary battery monitor. It does NOT implement the
# BatteryMonitorBackend interface and does NOT compute SoC. Its only job
# is to opportunistically read the connected Victron device (typically a
# Blue Smart IP22 AC charger) and surface its state -- charging mode,
# output current, output voltage -- so the main loop can publish those
# values to MQTT alongside the real battery data coming from INA226.
#
# When the Victron is unreachable (e.g. mains down, BT congestion), the
# main loop simply gets None and carries on. INA226 is the source of
# truth for everything related to the battery itself.

class VictronChargerAux:
    """
    Auxiliary BLE reader for a Victron device (typically a Blue Smart
    IP22 AC charger). Returns charger state on demand; never blocks the
    main loop on failure.

    Required config keys (under monitoring.victron):
      ble_address     MAC address of the Victron device (e.g. C0:E1:2F:...)
      encryption_key  Per-device key from VictronConnect app
    """

    def __init__(self, cfg: dict):
        self._ble_address = cfg.get("ble_address", "")
        self._encryption_key = cfg.get("encryption_key", "")
        self._device_keys: dict = {}

    @property
    def name(self) -> str:
        return "Victron BLE (aux)"

    def initialize(self) -> bool:
        """Validate config and verify victron-ble is importable.

        Returns True if everything is set up, False otherwise. A False
        return is treated as a soft failure by the main loop -- the
        service still runs, just without charger telemetry.
        """
        try:
            import victron_ble.scanner  # noqa: F401
        except ImportError:
            print("[VICTRON-AUX] victron-ble not installed, charger "
                  "telemetry disabled")
            return False

        if not self._ble_address or self._ble_address == "AA:BB:CC:DD:EE:FF":
            print("[VICTRON-AUX] BLE address not configured, "
                  "charger telemetry disabled")
            return False

        if (not self._encryption_key
                or self._encryption_key == "0000000000000000"):
            print("[VICTRON-AUX] encryption key not configured, "
                  "charger telemetry disabled")
            return False

        self._device_keys = {self._ble_address: self._encryption_key}
        print(f"[VICTRON-AUX] Ready to read {self._ble_address}")
        return True

    def read(self, timeout_s: float = 3.0) -> Optional[dict]:
        """
        Scan for one Victron advertisement and return a small dict of
        charger fields, or None on any failure (timeout, parse error,
        etc). The caller treats a None as "no fresh data this cycle"
        and just doesn't update the charger fields.

        Returns dict with keys: voltage, current, state, error, source
        """
        try:
            from victron_ble.scanner import Scanner
            import asyncio
        except ImportError:
            return None

        target_mac = self._ble_address.lower()

        # One-shot Scanner subclass that captures the first matching
        # advertisement and signals via an asyncio.Event.
        class _OneShotScanner(Scanner):
            def __init__(self, keys, target):
                super().__init__(keys)
                self._target = target
                self.result = None
                self.done = asyncio.Event()

            def callback(self, ble_device, data, advertisement):
                if self.done.is_set():
                    return
                if ble_device.address.lower() != self._target:
                    return
                try:
                    device = self.get_device(ble_device, data)
                    self.result = device.parse(data)
                except Exception as exc:  # noqa: BLE001
                    print(f"[VICTRON-AUX] Parse error: {exc}")
                    return
                self.done.set()

        async def _scan_once():
            scanner = _OneShotScanner(self._device_keys, target_mac)
            await scanner.start()
            try:
                await asyncio.wait_for(scanner.done.wait(), timeout=timeout_s)
            finally:
                await scanner.stop()
            return scanner.result

        loop = asyncio.new_event_loop()
        try:
            try:
                parsed = loop.run_until_complete(_scan_once())
            except asyncio.TimeoutError:
                # Charger off (mains down) or out of range -- normal during
                # outages. Don't spam the log.
                return None
        finally:
            loop.close()

        if parsed is None:
            return None

        # Extract a uniform dict regardless of device type.
        # Voltage/current: try battery getters first, then output channel 1.
        voltage = None
        current = None
        for g in ("get_voltage", "get_battery_voltage", "get_output_voltage1"):
            if hasattr(parsed, g):
                v = getattr(parsed, g)()
                if v is not None:
                    voltage = float(v)
                    break
        for g in ("get_current", "get_battery_current", "get_output_current1"):
            if hasattr(parsed, g):
                c = getattr(parsed, g)()
                if c is not None:
                    current = float(c)
                    break

        # Charge state and error are typically enums; render as lowercase
        # strings so they're MQTT/HA friendly.
        state = None
        if hasattr(parsed, "get_charge_state"):
            cs = parsed.get_charge_state()
            if cs is not None:
                state = getattr(cs, "name", str(cs)).lower()
        error = None
        if hasattr(parsed, "get_charger_error"):
            ce = parsed.get_charger_error()
            if ce is not None:
                error = getattr(ce, "name", str(ce)).lower()

        return {
            "voltage": voltage,
            "current": current,
            "state": state,
            "error": error,
            "source": f"victron-{type(parsed).__name__.lower()}",
        }

    def close(self):
        print("[VICTRON-AUX] Closed")



# =============================================================================
# Factory
# =============================================================================

def create_monitor_backend(
    cfg: dict,
) -> "tuple[BatteryMonitorBackend, Optional[VictronChargerAux]]":
    """
    Build the monitoring stack.

    Returns a (primary, aux) tuple:
      - primary : INA226 battery monitor (REQUIRED). This is the source of
                  truth for voltage / current / SoC.
      - aux     : VictronChargerAux when monitoring.victron is configured,
                  else None. Provides charger-side telemetry only.

    Configuration:
      monitoring:
        ina226:
          i2c_bus: 1
          address: "0x40"
          shunt_resistor: 0.01
        victron:                      # optional
          ble_address: "C0:E1:2F:..."
          encryption_key: "..."
    """
    monitoring_cfg = cfg.get("monitoring", {})
    battery_cfg = cfg.get("battery", {})

    primary = INA226Backend(monitoring_cfg.get("ina226", {}), battery_cfg)

    aux: Optional[VictronChargerAux] = None
    victron_cfg = monitoring_cfg.get("victron")
    if victron_cfg:
        candidate = VictronChargerAux(victron_cfg)
        # initialize() returns False on missing config / missing library;
        # in that case the aux is silently dropped (warning already logged).
        if candidate.initialize():
            aux = candidate

    return primary, aux
