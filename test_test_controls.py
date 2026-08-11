"""Behavioural check of the functional test controls."""
import os, sys
from unittest.mock import MagicMock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_controls as TC


def make(has_test_level=True):
    pub = {}
    c = MagicMock()
    c.publish.side_effect = lambda t, p, retain=False: pub.__setitem__(t, p)
    pump = MagicMock()
    pump.test_plan_active = False

    def set_test_plan(enabled):
        # Mirrors PumpController: refuses to enable without a test_level
        if enabled and not has_test_level:
            return False
        pump.test_plan_active = enabled
        return True

    pump.set_test_plan.side_effect = set_test_plan
    net = MagicMock()
    return TC.TestControls(c, {"device_name": "reef_battery",
                               "base_topic": "homeassistant"},
                           {"identifiers": ["reef_battery"]}, pump, net), c, pub, pump, net


SW = "homeassistant/switch/reef_battery_test_plan/state"
NB = "homeassistant/number/reef_battery_wifi_cut_min/state"

# switch on / off
tc, c, pub, pump, net = make()
msg = MagicMock(); msg.payload = b"ON"
tc._on_switch(None, None, msg)
assert pub[SW] == "ON", pub[SW]
msg.payload = b"OFF"
tc._on_switch(None, None, msg)
assert pub[SW] == "OFF"
print("switch ON/OFF drives set_test_plan            OK")

# refusal is reflected back: HA must not show a state that is a lie
tc, c, pub, pump, net = make(has_test_level=False)
msg.payload = b"ON"
tc._on_switch(None, None, msg)
assert pub[SW] == "OFF", "refused enable must republish OFF"
print("refused enable republishes OFF                OK")

# garbage ignored
tc, c, pub, pump, net = make()
msg.payload = b"MAYBE"
tc._on_switch(None, None, msg)
assert not pump.set_test_plan.called
print("invalid switch payload ignored                OK")

# wifi cut triggers the network helper and clamps
tc, c, pub, pump, net = make()
msg.payload = b"5"
tc._on_number(None, None, msg)
net.cut_wifi_for.assert_called_once_with(5.0)
assert pub[NB] == "5"
tc._reset_timer.cancel()

msg.payload = b"999"
tc._on_number(None, None, msg)
assert net.cut_wifi_for.call_args.args[0] == TC.MAX_WIFI_CUT_MIN
tc._reset_timer.cancel()
print(f"wifi cut called and clamped to {TC.MAX_WIFI_CUT_MIN} min       OK")

# 0 cancels without calling the helper again
calls = net.cut_wifi_for.call_count
msg.payload = b"0"
tc._on_number(None, None, msg)
assert net.cut_wifi_for.call_count == calls
assert pub[NB] == "0"
print("0 resets without cutting                      OK")

# invalid duration ignored
msg.payload = b"soon"
tc._on_number(None, None, msg)
assert net.cut_wifi_for.call_count == calls
print("invalid duration ignored                      OK")

# the number returns to 0 on its own
tc, c, pub, pump, net = make()
msg.payload = b"1"
tc._on_number(None, None, msg)
assert pub[NB] == "1"
tc._reset_timer.cancel()
tc._reset_cut()
assert pub[NB] == "0"
print("number returns to 0 after the cut             OK")

# state mirrors a test mode that ended on its own
tc, c, pub, pump, net = make()
pump.test_plan_active = True
tc.publish_state()
assert pub[SW] == "ON"
pump.test_plan_active = False
tc.publish_state()
assert pub[SW] == "OFF"
print("state follows an externally ended test mode   OK")
