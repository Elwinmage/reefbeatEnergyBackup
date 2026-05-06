"""
Power outage detection backends.

Two methods:
  - Relay: 230V relay on GPIO (Finder 40.61.8.230.4000)
  - Monitor: detect from battery current direction (INA226 or Victron)
"""

import time
import threading
from enum import Enum
from typing import Optional, Callable

try:
    import RPi.GPIO as GPIO
    RPI_AVAILABLE = True
except ImportError:
    RPI_AVAILABLE = False


class PowerState(Enum):
    MAINS = "mains"
    BATTERY = "battery"


class OutageDetector:
    """Abstract outage detector interface."""

    def __init__(self):
        self.state = PowerState.MAINS
        self._callback: Optional[Callable] = None

    def on_change(self, callback: Callable):
        """Register callback(old_state, new_state)."""
        self._callback = callback

    def _notify(self, old: PowerState, new: PowerState):
        if self._callback and old != new:
            self._callback(old, new)

    def cleanup(self):
        pass


# =============================================================================
# Relay-based detection (Finder 40.61.8.230.4000)
# =============================================================================

class RelayDetector(OutageDetector):
    """
    Detects outage via 230V relay NO contact on GPIO.
    
    Wiring:
      - Relay coil (A1/A2): Phase + Neutral from 230V mains
      - Relay NO contact: one side to GPIO pin, other to GND
      - 10k pull-up resistor between GPIO pin and 3.3V
      
    Logic (active_low, default):
      - Mains OK  -> coil energized -> NO closed -> GPIO LOW
      - Power out -> coil drops    -> NO open   -> GPIO HIGH (pull-up)
    
    Logic (active_high):
      - Inverted: uses NC contact instead of NO
    """

    def __init__(self, cfg: dict):
        super().__init__()
        self._pin = cfg.get("gpio_pin", 17)
        self._debounce_ms = cfg.get("debounce_ms", 200)
        self._active_low = cfg.get("logic", "active_low") == "active_low"
        self._last_change = 0.0

        if RPI_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.state = self._read()
            GPIO.add_event_detect(
                self._pin, GPIO.BOTH,
                callback=self._on_edge,
                bouncetime=self._debounce_ms,
            )
            print(f"[RELAY] GPIO{self._pin} ready, state: {self.state.value}")
        else:
            print("[RELAY] GPIO unavailable, assuming mains")

    def _read(self) -> PowerState:
        """Read current state from GPIO pin."""
        pin_high = GPIO.input(self._pin) == GPIO.HIGH
        if self._active_low:
            # Active low: HIGH = outage (relay open)
            return PowerState.BATTERY if pin_high else PowerState.MAINS
        else:
            # Active high: LOW = outage
            return PowerState.MAINS if pin_high else PowerState.BATTERY

    def _on_edge(self, channel):
        """GPIO interrupt callback with debounce."""
        now = time.monotonic()
        if now - self._last_change < self._debounce_ms / 1000.0:
            return
        self._last_change = now

        new = self._read()
        if new != self.state:
            old = self.state
            self.state = new
            print(f"[RELAY] {old.value} -> {new.value}")
            self._notify(old, new)

    def cleanup(self):
        if RPI_AVAILABLE:
            GPIO.cleanup(self._pin)
            print("[RELAY] GPIO cleaned up")


# =============================================================================
# Monitor-based detection (from INA226 or Victron current reading)
# =============================================================================

class MonitorDetector(OutageDetector):
    """
    Detects outage from battery current direction.
    
    When battery switches from charging (negative current)
    to discharging (positive current), it means mains power
    is lost and the battery is now supplying the load.
    
    Uses a confirmation delay to avoid false triggers from
    transient current spikes.
    """

    def __init__(self, cfg: dict):
        super().__init__()
        self._threshold = cfg.get("current_threshold_a", 0.05)
        self._confirm_delay = cfg.get("confirm_delay_s", 2.0)
        self._pending_state: Optional[PowerState] = None
        self._pending_since: float = 0.0
        print(f"[MONITOR-DET] Threshold={self._threshold}A, "
              f"confirm={self._confirm_delay}s")

    def update(self, current: float):
        """
        Call this every poll cycle with the current reading.
        Positive current = discharging = on battery.
        Negative current = charging = on mains.
        """
        if current > self._threshold:
            candidate = PowerState.BATTERY
        elif current < -self._threshold:
            candidate = PowerState.MAINS
        else:
            # In dead zone, don't change state
            return

        if candidate != self.state:
            now = time.monotonic()
            if self._pending_state == candidate:
                # Confirm after delay
                if now - self._pending_since >= self._confirm_delay:
                    old = self.state
                    self.state = candidate
                    self._pending_state = None
                    print(f"[MONITOR-DET] {old.value} -> {candidate.value} (confirmed)")
                    self._notify(old, candidate)
            else:
                # Start confirmation timer
                self._pending_state = candidate
                self._pending_since = now
        else:
            # State matches, clear pending
            self._pending_state = None


# =============================================================================
# Factory
# =============================================================================

def create_outage_detector(cfg: dict) -> OutageDetector:
    """Create the appropriate outage detector from config."""
    detection_cfg = cfg.get("outage_detection", {})
    method = detection_cfg.get("method", "relay")

    if method == "relay":
        return RelayDetector(detection_cfg.get("relay", {}))
    elif method == "monitor":
        return MonitorDetector(detection_cfg.get("monitor", {}))
    else:
        raise ValueError(f"Unknown outage detection method: {method}")
