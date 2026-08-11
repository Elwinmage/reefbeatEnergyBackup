"""
Functional-test controls exposed to Home Assistant.

Two capabilities already existed in the service but had no entity, so the
reef_battery_test blueprint referenced `switch.reef_battery_test_plan` and
`number.reef_battery_wifi_cut_min` while nothing ever created them: both
optional steps of the blueprint silently did nothing.

  switch  test_plan     -> PumpController.set_test_plan()
                           applies the configured `test_level` (reduced speed,
                           optionally one pump off) so the control path can be
                           verified without waiting for a real outage.

  number  wifi_cut_min  -> NetworkManager.cut_wifi_for()
                           drops the Wi-Fi link for N minutes to exercise the
                           failover chain. Acts as a trigger: it returns to 0
                           on its own once the link is restored.

Both carry a `reef_role` attribute so the blueprint resolves them from the
device rather than from a hardcoded entity_id, like the battery sensors.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

ROLE_TEST_PLAN = "test_plan"
ROLE_WIFI_CUT = "wifi_cut_min"

MAX_WIFI_CUT_MIN = 30


class TestControls:
    """Expose the pump-command test and the Wi-Fi cut as HA entities."""

    def __init__(self, client, mqtt_cfg: dict, device_info: dict,
                 pump, network):
        self._client = client
        self._device = mqtt_cfg.get("device_name", "reef_battery")
        self._base = mqtt_cfg.get("base_topic", "homeassistant")
        self._device_info = device_info
        self._pump = pump
        self._network = network

        # Minutes currently requested; back to 0 once the link is restored.
        self._wifi_cut_pending: float = 0.0
        self._reset_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------

    @property
    def _switch_uid(self) -> str:
        return f"{self._device}_test_plan"

    @property
    def _number_uid(self) -> str:
        return f"{self._device}_wifi_cut_min"

    def _topic(self, component: str, uid: str, leaf: str) -> str:
        return f"{self._base}/{component}/{uid}/{leaf}"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def publish_discovery(self) -> None:
        if not self._client:
            return

        switch = {
            "name": "Test commandes pompes",
            "unique_id": self._switch_uid,
            "device": self._device_info,
            "state_topic": self._topic("switch", self._switch_uid, "state"),
            "command_topic": self._topic("switch", self._switch_uid, "command"),
            "json_attributes_topic": self._topic(
                "switch", self._switch_uid, "attributes"),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "config",
            "icon": "mdi:pump",
        }

        number = {
            "name": "Coupure Wi-Fi (test)",
            "unique_id": self._number_uid,
            "device": self._device_info,
            "state_topic": self._topic("number", self._number_uid, "state"),
            "command_topic": self._topic("number", self._number_uid, "command"),
            "json_attributes_topic": self._topic(
                "number", self._number_uid, "attributes"),
            "min": 0,
            "max": MAX_WIFI_CUT_MIN,
            "step": 1,
            "mode": "box",
            "unit_of_measurement": "min",
            "entity_category": "config",
            "icon": "mdi:wifi-off",
        }

        for component, uid, payload in (
            ("switch", self._switch_uid, switch),
            ("number", self._number_uid, number),
        ):
            self._client.publish(
                self._topic(component, uid, "config"),
                json.dumps(payload), retain=True,
            )
            time.sleep(0.05)

        print("[TESTCTL] Published test-control entity discovery")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def publish_state(self) -> None:
        """Mirror the real state.

        Republished every cycle because test mode can also end on its own
        (restore_normal after an outage), and HA must not keep showing the
        switch as on.
        """
        if not self._client:
            return

        active = bool(getattr(self._pump, "test_plan_active", False))
        self._client.publish(
            self._topic("switch", self._switch_uid, "state"),
            "ON" if active else "OFF", retain=True,
        )
        self._client.publish(
            self._topic("switch", self._switch_uid, "attributes"),
            json.dumps({"reef_role": ROLE_TEST_PLAN}), retain=True,
        )
        self._client.publish(
            self._topic("number", self._number_uid, "state"),
            str(int(self._wifi_cut_pending)), retain=True,
        )
        self._client.publish(
            self._topic("number", self._number_uid, "attributes"),
            json.dumps({"reef_role": ROLE_WIFI_CUT}), retain=True,
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._client:
            return
        handlers = {
            self._topic("switch", self._switch_uid, "command"): self._on_switch,
            self._topic("number", self._number_uid, "command"): self._on_number,
        }
        for topic, handler in handlers.items():
            self._client.subscribe(topic)
            self._client.message_callback_add(topic, handler)

        self.publish_discovery()
        self.publish_state()

    def _on_switch(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="replace").strip().upper()
        if payload not in ("ON", "OFF"):
            print(f"[TESTCTL] Ignored invalid switch payload: {payload!r}")
            return
        self._pump.set_test_plan(payload == "ON")
        # Publish what actually happened, not what was asked: set_test_plan
        # refuses to enable when no `test_level` is configured, and HA must
        # show the switch flipping back rather than a state that is a lie.
        self.publish_state()

    def _on_number(self, client, userdata, msg) -> None:
        try:
            minutes = float(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            print(f"[TESTCTL] Ignored invalid cut duration: {msg.payload!r}")
            return
        minutes = max(0.0, min(float(MAX_WIFI_CUT_MIN), minutes))

        if minutes <= 0:
            self._wifi_cut_pending = 0.0
            self.publish_state()
            return

        self._wifi_cut_pending = minutes
        self.publish_state()
        self._network.cut_wifi_for(minutes)

        # Return the number to 0 once the link is back, so it reads as a
        # trigger rather than a setting. The publish only reaches the broker
        # after the link is restored anyway, which is precisely when it is
        # true. A small margin covers the interface coming back up.
        if self._reset_timer is not None:
            self._reset_timer.cancel()
        self._reset_timer = threading.Timer(minutes * 60.0 + 15.0, self._reset_cut)
        self._reset_timer.daemon = True
        self._reset_timer.start()

    def _reset_cut(self) -> None:
        self._wifi_cut_pending = 0.0
        self.publish_state()

    def stop(self) -> None:
        if self._reset_timer is not None:
            self._reset_timer.cancel()
