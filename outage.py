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
    from gpiozero import DigitalInputDevice
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
# On définit l'Enum PowerState pour que la comparaison
# detector.state == PowerState.BATTERY fonctionne
class RelayDetector:
    def __init__(self, config):
        self._pin = config.get("gpio_pin", 17)
        self._debounce = config.get("debounce_ms", 200) / 1000.0
        
        # Initialisation de l'entrée
        self._device = DigitalInputDevice(
            self._pin, 
            pull_up=True, 
            bounce_time=self._debounce
        )

        self._callback = None
        self._last_state = self.state  # Initialize with current state

        # Callbacks de gpiozero
        self._device.when_activated = self._internal_handler
        self._device.when_deactivated = self._internal_handler
        
        print(f"[OUTAGE] RelayDetector initialisé sur BCM {self._pin}")

    @property
    def state(self):
        """
        Cette propriété permet à main.py de faire 'detector.state'.
        Elle renvoie PowerState.BATTERY si le courant est coupé.
        """
        # Si .is_active est False, le relais indique une coupure (selon ton câblage)
        if not self._device.is_active:
            return PowerState.MAINS
        else:
            return PowerState.BATTERY

    def on_change(self, callback_func):
        """Enregistre la fonction à appeler lors d'un changement d'état"""
        self._callback = callback_func

    def read(self):
        """Renvoie True si coupure, False si secteur"""
        return self.state == PowerState.BATTERY

    def _internal_handler(self):
        """Déclenché par le changement physique du signal"""
        new_state = self.state
        if self._callback and new_state != self._last_state:
            old_state = self._last_state
            self._last_state = new_state
            # On envoie (old, new) pour correspondre à la signature OutageDetector
            self._callback(old_state, new_state)

    def cleanup(self):
        self._device.close()

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
