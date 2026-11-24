"""Device triggers."""

from __future__ import annotations

import json

from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.open_parcels.const import DOMAIN
from custom_components.open_parcels.device import find_device
from custom_components.open_parcels.device_trigger import _is_hub

from . import payloads
from .conftest import FakeStream


async def test_device_triggers(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """Connection triggers fire for that connection; hub triggers for every parcel."""
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    gls = find_device(
        hass,
        loaded.entry_id,
        (DOMAIN, f"{loaded.entry_id}_connection_{payloads.CONNECTION_GLS}"),
    )
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, gls.id
    )
    types = {t["type"] for t in triggers if t["domain"] == DOMAIN}
    assert "parcel_delivered" in types and "connection_state_changed" in types

    calls = async_mock_service(hass, "test", "record")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device.id,
                        "type": "parcel_status_changed",
                    },
                    "action": {"service": "test.record", "data": {"who": who}},
                }
                for device, who in ((gls, "gls"), (hub, "hub"))
            ]
        },
    )
    parcel = payloads.parcels()[1]
    parcel["status"] = "returning"
    stream.push("parcel.status_changed", json.dumps({"parcel": parcel}), "90")
    for _ in range(3):
        await hass.async_block_till_done()
    assert sorted(call.data["who"] for call in calls) == ["gls", "hub"]

    # An unknown device id is not the hub.
    assert not _is_hub(hass, "missing")
