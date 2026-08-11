"""
Battery discharge test exposed as a ha-reef-card maintenance task.

ha-reef-card builds its maintenance view by scanning Home Assistant for
entities carrying a `reef_role` attribute, not by knowing which integration
published them (see src/utils/maintenance.ts in that repo). Three entities on
the same HA device make one task:

  button.*  reef_role = "maint_<task>"                  the action + the state
  number.*  reef_role = "maint_<task>_interval_<unit>"   the editable interval
  switch.*  reef_role = "maint_<task>_notify"            the per-task mute

The button carries every computed attribute, because the card treats it as the
single source of truth:

  task_key      catalogue key
  interval_days interval normalised to days
  days_left     remaining days, negative once overdue, null when never run
  overdue       boolean
  last_reset    ISO-8601 date of the last run, or null
  notify        mirror of the companion switch

Publishing those three entities over MQTT discovery is therefore enough for the
battery test to appear in the card next to the Red Sea tasks, with no change on
the card side.

Ownership of the interval
-------------------------
The schedule lives in the `reef_battery_test` blueprint, but a blueprint cannot
be read from here: this service only speaks MQTT. So the direction is inverted
-- the interval number published here is the single source of truth, and the
blueprint reads it at trigger time instead of holding its own static input.
That keeps the card's inline interval editor meaningful: dragging the slider
really does change when the next test runs.

The blueprint presses the button when a test completes, which is what resets
the countdown.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Task identity. Changing these renames the entities in Home Assistant.
TASK_KEY = "battery_discharge_test"
ROLE = f"maint_{TASK_KEY}"
INTERVAL_UNIT = "months"  # must be one of days/weeks/months for the card

# The card displays the slider in months but computes progress from
# interval_days, so this module has to do the conversion itself. 30 is this
# module's convention; ha-reefbeat-component uses its own factor for its own
# tasks, which only matters if you compare the two side by side.
DAYS_PER_MONTH = 30

DEFAULT_INTERVAL_MONTHS = 3
MIN_INTERVAL_MONTHS = 1
MAX_INTERVAL_MONTHS = 12


class BatteryTestMaintenance:
    """Publish and maintain the battery-test task over MQTT discovery."""

    def __init__(
        self,
        client,
        mqtt_cfg: dict,
        device_info: dict,
        persist_path: str = "/var/lib/reefbeat-energy-backup/maintenance.json",
    ):
        self._client = client
        self._device = mqtt_cfg.get("device_name", "reef_battery")
        self._base = mqtt_cfg.get("base_topic", "homeassistant")
        self._device_info = device_info
        self._path = Path(persist_path)

        self._lock = threading.Lock()
        self._last_reset: Optional[str] = None
        self._interval_months: int = DEFAULT_INTERVAL_MONTHS
        self._notify: bool = True

        self._load()

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    @property
    def _button_uid(self) -> str:
        return f"{self._device}_{TASK_KEY}"

    @property
    def _number_uid(self) -> str:
        return f"{self._device}_{TASK_KEY}_interval_{INTERVAL_UNIT}"

    @property
    def _switch_uid(self) -> str:
        return f"{self._device}_{TASK_KEY}_notify"

    def _topic(self, component: str, uid: str, leaf: str) -> str:
        return f"{self._base}/{component}/{uid}/{leaf}"

    # ------------------------------------------------------------------
    # Derived state
    # ------------------------------------------------------------------

    @property
    def interval_days(self) -> int:
        return self._interval_months * DAYS_PER_MONTH

    def _days_left(self) -> Optional[int]:
        """Days until the next test, negative once overdue.

        None when the test has never run: the card renders that as "never
        done" rather than as overdue, which would be misleading on a fresh
        install.
        """
        if not self._last_reset:
            return None
        try:
            last = date.fromisoformat(self._last_reset[:10])
        except ValueError:
            return None
        elapsed = (date.today() - last).days
        return self.interval_days - elapsed

    def attributes(self) -> dict:
        """Attribute payload of the button, i.e. what the card reads."""
        days_left = self._days_left()
        return {
            "reef_role": ROLE,
            "task_key": TASK_KEY,
            "interval_days": self.interval_days,
            "days_left": days_left,
            "overdue": days_left is not None and days_left < 0,
            "last_reset": self._last_reset,
            "notify": self._notify,
            "icon": "mdi:battery-clock",
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def publish_discovery(self) -> None:
        """Declare the three entities to Home Assistant.

        Published directly rather than through the MQTT buffer: unlike the
        sensor stream, a task that appears a few minutes late costs nothing,
        and retain=True means HA picks it up whenever it reconnects.
        """
        if not self._client:
            return

        attrs_topic = self._topic("button", self._button_uid, "attributes")

        button = {
            "name": "Test de decharge batterie",
            "unique_id": self._button_uid,
            "device": self._device_info,
            "command_topic": self._topic("button", self._button_uid, "command"),
            "payload_press": "PRESS",
            # The card reads everything from these attributes.
            "json_attributes_topic": attrs_topic,
            "entity_category": "config",
            "icon": "mdi:battery-clock",
        }

        number = {
            "name": "Test batterie intervalle",
            "unique_id": self._number_uid,
            "device": self._device_info,
            "state_topic": self._topic("number", self._number_uid, "state"),
            "command_topic": self._topic("number", self._number_uid, "command"),
            "json_attributes_topic": self._topic(
                "number", self._number_uid, "attributes"
            ),
            "min": MIN_INTERVAL_MONTHS,
            "max": MAX_INTERVAL_MONTHS,
            "step": 1,
            "mode": "slider",
            "unit_of_measurement": INTERVAL_UNIT,
            "entity_category": "config",
            "icon": "mdi:calendar-sync",
        }

        switch = {
            "name": "Test batterie notifications",
            "unique_id": self._switch_uid,
            "device": self._device_info,
            "state_topic": self._topic("switch", self._switch_uid, "state"),
            "command_topic": self._topic("switch", self._switch_uid, "command"),
            "json_attributes_topic": self._topic(
                "switch", self._switch_uid, "attributes"
            ),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "config",
            "icon": "mdi:bell",
        }

        for component, uid, payload in (
            ("button", self._button_uid, button),
            ("number", self._number_uid, number),
            ("switch", self._switch_uid, switch),
        ):
            self._client.publish(
                self._topic(component, uid, "config"),
                json.dumps(payload),
                retain=True,
            )
            time.sleep(0.05)

        print("[MAINT] Published battery-test maintenance task discovery")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def publish_state(self) -> None:
        """Republish the three entity states and the button attributes.

        Called on every service cycle: `days_left` is derived from today's
        date, so it has to be recomputed rather than pushed only on change.
        """
        if not self._client:
            return

        with self._lock:
            attrs = json.dumps(self.attributes())
            months = self._interval_months
            notify = self._notify

        self._client.publish(
            self._topic("button", self._button_uid, "attributes"),
            attrs, retain=True,
        )
        self._client.publish(
            self._topic("number", self._number_uid, "state"),
            str(months), retain=True,
        )
        self._client.publish(
            self._topic("number", self._number_uid, "attributes"),
            json.dumps({"reef_role": f"{ROLE}_interval_{INTERVAL_UNIT}"}),
            retain=True,
        )
        self._client.publish(
            self._topic("switch", self._switch_uid, "state"),
            "ON" if notify else "OFF", retain=True,
        )
        self._client.publish(
            self._topic("switch", self._switch_uid, "attributes"),
            json.dumps({"reef_role": f"{ROLE}_notify"}),
            retain=True,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to the command topics and publish the initial state."""
        if not self._client:
            return

        handlers = {
            self._topic("button", self._button_uid, "command"): self._on_press,
            self._topic("number", self._number_uid, "command"): self._on_interval,
            self._topic("switch", self._switch_uid, "command"): self._on_notify,
        }
        for topic, handler in handlers.items():
            self._client.subscribe(topic)
            self._client.message_callback_add(topic, handler)

        self.publish_discovery()
        self.publish_state()

    def _on_press(self, client, userdata, msg) -> None:
        """Button pressed: record the test as done today."""
        self.mark_done()

    def _on_interval(self, client, userdata, msg) -> None:
        try:
            months = int(float(msg.payload.decode()))
        except (ValueError, UnicodeDecodeError):
            print(f"[MAINT] Ignored invalid interval: {msg.payload!r}")
            return
        months = max(MIN_INTERVAL_MONTHS, min(MAX_INTERVAL_MONTHS, months))
        with self._lock:
            self._interval_months = months
            self._save_locked()
        print(f"[MAINT] Test interval set to {months} months")
        self.publish_state()

    def _on_notify(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="replace").strip().upper()
        if payload not in ("ON", "OFF"):
            print(f"[MAINT] Ignored invalid notify payload: {payload!r}")
            return
        with self._lock:
            self._notify = payload == "ON"
            self._save_locked()
        self.publish_state()

    def mark_done(self, when: Optional[datetime] = None) -> None:
        """Reset the countdown, as the blueprint does when a test finishes."""
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._last_reset = stamp
            self._save_locked()
        print(f"[MAINT] Battery test marked done at {stamp}")
        self.publish_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data: dict[str, Any] = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        self._last_reset = data.get("last_reset") or None
        try:
            months = int(data.get("interval_months", DEFAULT_INTERVAL_MONTHS))
            self._interval_months = max(
                MIN_INTERVAL_MONTHS, min(MAX_INTERVAL_MONTHS, months)
            )
        except (TypeError, ValueError):
            self._interval_months = DEFAULT_INTERVAL_MONTHS
        self._notify = bool(data.get("notify", True))

    def _save_locked(self) -> None:
        """Persist state. The caller must already hold the lock."""
        payload = {
            "last_reset": self._last_reset,
            "interval_months": self._interval_months,
            "notify": self._notify,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename: a power cut mid-write must not leave a
            # truncated file behind, this service exists for power cuts.
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path)
        except OSError as e:
            print(f"[MAINT] Could not persist state: {e}")
