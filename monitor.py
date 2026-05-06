"""
Battery monitoring backends.

Provides a unified interface for reading battery voltage, current,
and computing SoC. Two backends available:
  - INA226: I2C shunt monitor (direct hardware)
  - Victron: BLE connection to Victron Blue Smart IP22 charger
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# SoC estimator (shared by all backends)
# =============================================================================

# LiFePO4 8S voltage-to-SoC lookup (pack_voltage, soc_percent)
_SOC_TABLE = [
    (29.20, 100), (27.20, 99), (26.80, 90), (26.56, 70),
    (26.40, 40), (26.24, 30), (26.00, 20), (25.60, 17),
    (24.80, 14), (24.00, 9), (22.40, 0),
]


def voltage_to_soc(v: float) -> float:
    """Estimate SoC from pack voltage using linear interpolation."""
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
    """Track SoC by integrating current over time."""
    capacity_ah: float
    soc: float = 100.0
    _last_time: Optional[float] = field(default=None, repr=False)

    def update(self, current: float, voltage: float) -> float:
        now = time.monotonic()
        if self._last_time is not None:
            dt_h = (now - self._last_time) / 3600.0
            self.soc -= (current * dt_h / self.capacity_ah) * 100.0
            self.soc = max(0.0, min(100.0, self.soc))
        self._last_time = now
        # Blend with voltage estimate to correct drift
        self.soc = self.soc * 0.98 + voltage_to_soc(voltage) * 0.02
        return self.soc


# =============================================================================
# Battery reading data class
# =============================================================================

@dataclass
class BatteryReading:
    """Unified battery reading from any backend."""
    voltage: float = 0.0
    current: float = 0.0       # Positive = discharging, negative = charging
    power: float = 0.0
    soc: float = 100.0
    is_charging: bool = False
    is_discharging: bool = False
    source: str = "unknown"


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
    def read(self) -> BatteryReading:
        """Read current battery state."""
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
        try:
            import smbus2
            self._bus = smbus2.SMBus(self._bus_num)
            # Configure: continuous shunt+bus, 1.1ms, 16 averages
            self._write_reg(self._REG_CONFIG, 0x4427)
            time.sleep(0.1)
            # Verify manufacturer ID
            mid = self._read_reg(self._REG_MANUFACTURER_ID)
            if mid == 0x5449:
                print(f"[INA226] Initialized (bus={self._bus_num}, "
                      f"addr=0x{self._address:02X}, shunt={self._shunt_ohms}Ω)")
                return True
            else:
                print(f"[INA226] WARNING: unexpected manufacturer ID 0x{mid:04X}")
                return True  # Still try to work
        except Exception as e:
            print(f"[INA226] Init failed: {e}")
            return False

    def read(self) -> BatteryReading:
        voltage = self._read_reg_signed(self._REG_BUS_VOLTAGE) * self._bus_v_lsb
        v_shunt = self._read_reg_signed(self._REG_SHUNT_VOLTAGE) * self._shunt_v_lsb
        current = v_shunt / self._shunt_ohms
        power = voltage * current
        soc = self._counter.update(current, voltage)

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

class VictronBLEBackend(BatteryMonitorBackend):
    """
    Battery monitoring via Victron Blue Smart IP22 charger BLE.
    
    Uses the victron_ble library to read charger data over
    Bluetooth Low Energy. The charger reports voltage, current,
    and charge state, which we use to derive battery SoC.
    
    Requirements:
      pip install victron-ble
    """

    def __init__(self, cfg: dict, battery_cfg: dict):
        self._ble_address = cfg.get("ble_address", "")
        self._encryption_key = cfg.get("encryption_key", "")
        self._poll_interval = cfg.get("poll_interval_s", 5.0)
        self._scanner = None
        self._last_reading = BatteryReading(source="victron")
        self._counter = CoulombCounter(
            capacity_ah=battery_cfg.get("capacity_ah", 60.0),
            soc=battery_cfg.get("initial_soc", 100.0),
        )

    @property
    def name(self) -> str:
        return "Victron BLE"

    def initialize(self) -> bool:
        try:
            from victron_ble.scanner import Scanner
            from victron_ble.devices import detect_device_type

            print(f"[VICTRON] Initializing BLE ({self._ble_address})")

            if not self._ble_address or self._ble_address == "AA:BB:CC:DD:EE:FF":
                print("[VICTRON] ERROR: BLE address not configured")
                return False

            if not self._encryption_key or self._encryption_key == "0000000000000000":
                print("[VICTRON] ERROR: encryption key not configured")
                print("[VICTRON] Get it from VictronConnect app -> "
                      "Product info -> Show encryption key")
                return False

            self._device_keys = {self._ble_address: self._encryption_key}
            print("[VICTRON] Initialized successfully")
            return True

        except ImportError:
            print("[VICTRON] ERROR: victron-ble not installed")
            print("[VICTRON] Install with: pip install victron-ble")
            return False
        except Exception as e:
            print(f"[VICTRON] Init failed: {e}")
            return False

    def read(self) -> BatteryReading:
        """
        Read from Victron BLE advertisement data.
        
        The Victron charger broadcasts its state via BLE advertisements.
        We parse these to get voltage, current, and charge state.
        """
        try:
            from victron_ble.scanner import Scanner
            from victron_ble.devices import detect_device_type

            # Scan for a single advertisement
            import asyncio

            async def _scan_once():
                scanner = Scanner(self._device_keys)
                async for dev in scanner.scan():
                    if dev:
                        return dev
                return None

            # Run scan with timeout
            loop = asyncio.new_event_loop()
            try:
                device = loop.run_until_complete(
                    asyncio.wait_for(_scan_once(), timeout=10.0)
                )
            finally:
                loop.close()

            if device is None:
                print("[VICTRON] No data received")
                return self._last_reading

            # Extract data from Victron advertisement
            voltage = device.get_voltage() or 0.0
            current = device.get_current() or 0.0
            # Victron current sign: positive = charging into battery
            # We want: positive = discharging, so invert
            current = -current
            power = voltage * current
            soc = self._counter.update(current, voltage)

            self._last_reading = BatteryReading(
                voltage=round(voltage, 2),
                current=round(current, 3),
                power=round(power, 1),
                soc=round(soc, 1),
                is_charging=current < -0.05,
                is_discharging=current > 0.05,
                source="victron",
            )
            return self._last_reading

        except asyncio.TimeoutError:
            print("[VICTRON] BLE scan timeout")
            return self._last_reading
        except Exception as e:
            print(f"[VICTRON] Read error: {e}")
            return self._last_reading

    def close(self):
        print("[VICTRON] Closed")


# =============================================================================
# Factory
# =============================================================================

def create_monitor_backend(cfg: dict) -> BatteryMonitorBackend:
    """Create the appropriate monitoring backend from config."""
    monitoring_cfg = cfg.get("monitoring", {})
    battery_cfg = cfg.get("battery", {})
    backend_name = monitoring_cfg.get("backend", "ina226")

    if backend_name == "ina226":
        return INA226Backend(monitoring_cfg.get("ina226", {}), battery_cfg)
    elif backend_name == "victron":
        return VictronBLEBackend(monitoring_cfg.get("victron", {}), battery_cfg)
    else:
        raise ValueError(f"Unknown monitoring backend: {backend_name}")
