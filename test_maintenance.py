"""Behavioural check of the maintenance task module."""
import json, sys, os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maintenance as M

PATH = "/tmp/reef_maint_state_test.json"
if os.path.exists(PATH):
    os.remove(PATH)

def make():
    pub = {}
    c = MagicMock()
    c.publish.side_effect = lambda t, p, retain=False: pub.__setitem__(t, p)
    m = M.BatteryTestMaintenance(c, {"device_name": "reef_battery", "base_topic": "homeassistant"},
                                 {"identifiers": ["reef_battery"]}, persist_path=PATH)
    return m, c, pub

m, c, pub = make()
a = m.attributes()
assert a["days_left"] is None, a
assert a["overdue"] is False, "never-run must not read as overdue"
print("never run           -> days_left=None, overdue=False   OK")

m.mark_done(datetime.now(timezone.utc) - timedelta(days=100))
a = m.attributes()
assert a["days_left"] == -10, a
assert a["overdue"] is True
print(f"100d ago, 3 months  -> days_left={a['days_left']}, overdue=True   OK")

# interval change via MQTT command
msg = MagicMock(); msg.payload = b"6"
m._on_interval(None, None, msg)
a = m.attributes()
assert a["interval_days"] == 180 and a["days_left"] == 80, a
print(f"interval -> 6 months-> interval_days={a['interval_days']}, days_left={a['days_left']}   OK")

# out-of-range and garbage are clamped / ignored
for payload, expect in ((b"99", 12), (b"0", 1), (b"abc", 1)):
    msg.payload = payload
    m._on_interval(None, None, msg)
    assert m._interval_months == expect, (payload, m._interval_months)
print("clamping / garbage  -> 99->12, 0->1, 'abc' ignored     OK")

# notify switch
msg.payload = b"OFF"
m._on_notify(None, None, msg)
assert m.attributes()["notify"] is False
msg.payload = b"garbage"
m._on_notify(None, None, msg)
assert m.attributes()["notify"] is False, "invalid payload must not flip the switch"
print("notify OFF, garbage ignored                            OK")

# persistence across restart
m2, _, _ = make()
assert m2._interval_months == 1 and m2._notify is False and m2._last_reset
print("state survives restart                                 OK")

# button press resets
m2._on_press(None, None, MagicMock())
assert m2.attributes()["days_left"] == m2.interval_days
print("button press resets the countdown                      OK")

# corrupt file must not crash startup
open(PATH, "w").write("{not json")
m3, _, _ = make()
assert m3._interval_months == M.DEFAULT_INTERVAL_MONTHS
print("corrupt state file falls back to defaults              OK")

# topics subscribed
m4, c4, _ = make()
m4.start()
subs = [call.args[0] for call in c4.subscribe.call_args_list]
assert len(subs) == 3 and all(s.endswith("/command") for s in subs), subs
print("subscribes to 3 command topics                         OK")
