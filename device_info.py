"""
Home Assistant device identity, shared by every MQTT discovery publisher.

All modules publish under the same `identifiers`, so Home Assistant merges
their payloads into a single device -- and the last message received wins for
`manufacturer` and `model`. Before this module the four publishers disagreed
(main.py said "KEPWORTH"/"LiFePO4 24V 60Ah", the others "reefbeat Backup" or
"reefbeat⚡Backup"/"Energy Backup System"), which made the device's own
manufacturer and model depend on publication order. That is invisible day to
day, but it breaks anything that filters on those fields -- notably a
blueprint device selector.

`model_id` is the stable, machine-readable key. It never changes, whatever the
battery or the display name, so it is the right thing to filter on when a
Home Assistant version supports it.
"""

from __future__ import annotations

from typing import Optional

MANUFACTURER = "reefbeat⚡Backup"
MODEL = "Energy Backup System"
MODEL_ID = "reefbeat-energy-backup"
DEVICE_LABEL = "Reef Battery Backup"


def build_device_info(device_name: str,
                      sw_version: Optional[str] = None) -> dict:
    """Return the `device` block of an MQTT discovery payload.

    device_name : the MQTT device_name from the config, used as identifier.
    sw_version  : optional version string, shown in the HA device page.
    """
    info = {
        "identifiers": [device_name],
        "name": DEVICE_LABEL,
        "manufacturer": MANUFACTURER,
        "model": MODEL,
        "model_id": MODEL_ID,
    }
    if sw_version:
        info["sw_version"] = sw_version
    return info
