import json, sys, yaml
from unittest.mock import MagicMock
from jinja2 import Environment
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import maintenance as M
from device_info import build_device_info
import main as MAIN

published = {}
class Buf:
    def publish(self, topic, payload, retain=False):
        published[topic] = payload
client = MagicMock()
client.publish.side_effect = lambda t, p, retain=False: published.__setitem__(t, p)

cfg = {"mqtt": {"device_name": "reef_battery", "base_topic": "homeassistant"},
       "monitoring": {"backend": "ina226"}}
MAIN.publish_ha_discovery(Buf(), cfg, has_victron=False)
m = M.BatteryTestMaintenance(client, cfg["mqtt"], build_device_info("reef_battery"),
                             persist_path="/tmp/x_maint.json")
m.publish_discovery(); m.publish_state()

from test_controls import TestControls
pump = MagicMock(); pump.test_plan_active = False
tc = TestControls(client, cfg["mqtt"], build_device_info("reef_battery"),
                  pump, MagicMock())
tc.publish_discovery(); tc.publish_state()

attrs_by_topic = {t: p for t, p in published.items() if t.endswith("/attributes")}
entities = {}
for topic, raw in published.items():
    if not topic.endswith("/config"):
        continue
    c = json.loads(raw)
    component = topic.split("/")[1]
    eid = f"{component}.{c['unique_id']}"
    a = {}
    if c.get("json_attributes_template"):
        a.update(json.loads(c["json_attributes_template"]))
    elif c.get("json_attributes_topic") in attrs_by_topic:
        a.update(json.loads(attrs_by_topic[c["json_attributes_topic"]]))
    entities[eid] = a

print(f"{len(entities)} entities published on the device")
print("roles:")
for eid, a in sorted(entities.items()):
    if a.get("reef_role"):
        print(f"   {a['reef_role']:48} {eid}")

class L(yaml.SafeLoader): pass
L.add_constructor("!input", lambda l, n: "<input>")
HERE = os.path.dirname(os.path.abspath(__file__))
bp = yaml.load(open(os.path.join(HERE, "blueprints", "reef_battery_test.yaml")), Loader=L)

env = Environment()
expected = {
    "v_soc_sensor_id": "sensor.reef_battery_soc",
    "v_voltage_sensor_id": "sensor.reef_battery_voltage",
    "v_power_sensor_id": "sensor.reef_battery_power",
    "v_maintenance_button": "button.reef_battery_battery_discharge_test",
    "v_test_period_entity": "number.reef_battery_battery_discharge_test_interval_months",
    "v_maintenance_notify_switch": "switch.reef_battery_battery_discharge_test_notify",
    "v_test_plan_switch_id": "switch.reef_battery_test_plan",
    "v_wifi_cut_number_id": "number.reef_battery_wifi_cut_min",
}
print("\nblueprint variable resolution:")
ok = True
for var, want in expected.items():
    got = env.from_string(bp["variables"][var]).render(
        v_backup_device="dev1",
        device_entities=lambda _: list(entities),
        state_attr=lambda e, a: entities.get(e, {}).get(a),
    ).strip()
    good = got == want
    ok &= good
    print(f"  {'OK ' if good else 'FAIL'} {var:30} -> {got or '(empty)'}")
print("\nALL RESOLVED" if ok else "\nRESOLUTION FAILED")
sys.exit(0 if ok else 1)
