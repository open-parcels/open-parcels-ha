"""Returns: the open-returns sensor, its bus events, device triggers and calendar entries.

Every id, shop and code here is made up.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.open_parcels.calendar import to_calendar_event
from custom_components.open_parcels.const import DOMAIN
from custom_components.open_parcels.device import find_device
from custom_components.open_parcels.texts import async_texts, cost_text, drop_off_text

from . import payloads
from .conftest import FakeStream, events

SENSOR = "sensor.open_parcels_open_returns"


async def _settle(hass: HomeAssistant) -> None:
    for _ in range(5):
        await hass.async_block_till_done()
        await asyncio.sleep(0)


def _return(**changes: Any) -> dict[str, Any]:
    ret = payloads.returns()[0]
    ret.update(changes)
    return ret


async def test_sensor_from_rest(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """The REST load fills the count and the compact list."""
    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "1"
    assert state.attributes["returns"] == [
        {
            "order_number": "ORDER-0001",
            "shop": "Example Shop",
            "items": ["Example shoes"],
            "status": "label_created",
            "return_by": "2999-02-01",
            "drop_off": ["Parcel locker (dhl)", "Any Example store"],
            "free": True,
            "cost": "Free return",
            "label_url": "https://shop.example.test/label/1",
        }
    ]


async def test_events_update_and_fire_once(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """Return events update the sensor and fire once per change, never on replays."""
    created = events(hass, f"{DOMAIN}_return_created")
    updated = events(hass, f"{DOMAIN}_return_updated")
    other = "12345678-1234-4234-8234-123456789012"
    new = _return(id=other, order_number="ORDER-0002", return_by="2999-01-15")
    stream.push("return.created", json.dumps({"return": new}), "100")
    await _settle(hass)
    state = hass.states.get(SENSOR)
    assert state.state == "2"
    # Soonest return-by day first.
    assert state.attributes["returns"][0]["order_number"] == "ORDER-0002"
    assert len(created) == 1
    assert created[0].data["return_id"] == other
    assert created[0].data["hub_device_id"] is not None

    # A replay of the same message, and a repeat with only a new timestamp: no event.
    stream.push("return.created", json.dumps({"return": new}), "100")
    later = {**new, "updated_at": "2999-01-01T11:00:00.000Z"}
    stream.push("return.updated", json.dumps({"return": later}), "101")
    stream.push("return.updated", json.dumps({"return": later}), "102")
    await _settle(hass)
    assert len(created) == 1
    assert len(updated) == 1

    # Dropped off: a new change fires; a stale (older) one is ignored.
    dropped = {**later, "status": "dropped_off", "updated_at": "2999-01-01T12:00:00Z"}
    stream.push("return.updated", json.dumps({"return": dropped}), "103")
    stale = {**later, "status": "requested", "updated_at": "2999-01-01T09:00:00Z"}
    stream.push("return.updated", json.dumps({"return": stale}), "104")
    await _settle(hass)
    assert [e.data["status"] for e in updated] == ["label_created", "dropped_off"]

    # Refunded: no longer open.
    done = {**dropped, "status": "refunded", "updated_at": "2999-01-02T12:00:00Z"}
    stream.push("return.updated", json.dumps({"return": done}), "105")
    stream.push("return.updated", json.dumps({"bogus": True}), "106")
    await _settle(hass)
    assert hass.states.get(SENSOR).state == "1"
    assert len(updated) == 3


async def test_order_deleted_drops_its_return(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """A deleted order takes its return along."""
    payload = {"id": payloads.RETURN_ORDER, "parcel_ids": []}
    stream.push("order.deleted", json.dumps(payload), "100")
    await _settle(hass)
    assert hass.states.get(SENSOR).state == "0"


async def test_return_triggers(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """Devices offer the return triggers."""
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, hub.id
    )
    types = {t["type"] for t in triggers if t["domain"] == DOMAIN}
    assert {"return_created", "return_updated"} <= types


async def test_calendar_return_entry(hass: HomeAssistant) -> None:
    """A return's entry names the item and lists drop-off, cost and label, in HA's language."""
    await hass.config.async_set_time_zone("UTC")
    entry = {
        "uid": "return-x@open-parcels",
        "parcel_id": None,
        "return_id": payloads.RETURN_OPEN,
        "order_id": payloads.RETURN_ORDER,
        "title": "Example shoes",
        "carrier": "dhl",
        "tracking_number": "ORDER-0001",
        "status": "label_created",
        "kind": "return_by",
        "drop_off": [
            {"carrier": "dhl", "kind": "parcel_locker", "description": None},
            {"carrier": None, "kind": "shop", "description": "Example Kiosk"},
        ],
        "cost": {"free": False, "amount": 4.5, "currency": "EUR"},
        "start": "2999-02-01T00:00:00.000Z",
        "end": "2999-02-02T00:00:00.000Z",
        "all_day": True,
        "start_date": "2999-02-01",
        "end_date": "2999-02-02",
        "late": False,
        "image_url": None,
        "url": "https://shop.example.test/label/1",
        "pickup_point": None,
    }
    event = to_calendar_event(entry, await async_texts(hass))
    assert event is not None
    assert event.summary == "Return by: Example shoes"
    assert event.description == (
        "Order: ORDER-0001\nDrop off: Parcel locker (dhl)\nDrop off: Example Kiosk\n"
        "Return costs 4.50 EUR\nStatus: Label ready\n"
        "Label: https://shop.example.test/label/1"
    )

    hass.config.language = "de"
    german = to_calendar_event(
        {**entry, "cost": {"free": True, "amount": None, "currency": None}},
        await async_texts(hass),
    )
    assert german is not None
    assert german.summary == "Zurücksenden bis: Example shoes"
    assert german.description == (
        "Bestellung: ORDER-0001\nAbgabe: Paketautomat (dhl)\nAbgabe: Example Kiosk\n"
        "Kostenlose Rücksendung\nStatus: Etikett bereit\n"
        "Etikett: https://shop.example.test/label/1"
    )


async def test_texts(hass: HomeAssistant) -> None:
    """Drop-off and cost words in HA's language; an unknown kind in the server's words."""
    texts = await async_texts(hass)
    assert (
        drop_off_text(texts, {"kind": "any", "carrier": None}) == "Any drop-off point"
    )
    assert drop_off_text(texts, {"kind": "new_kind"}) == "New kind"
    assert cost_text(texts, None) is None
    assert cost_text(texts, {"free": False, "amount": None}) == "Return costs money"
    assert cost_text(texts, {"free": False, "amount": 4, "currency": "EUR"}) == (
        "Return costs 4.00 EUR"
    )
    hass.config.language = "de"
    german = await async_texts(hass)
    assert drop_off_text(german, {"kind": "parcel_locker"}) == "Paketautomat"
    assert cost_text(german, {"free": False, "amount": 4, "currency": "EUR"}) == (
        "Rücksendung kostet 4.00 EUR"
    )


async def test_sensor_in_german(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """The sensor's drop-off and cost follow HA's language from the next full load."""
    hass.config.language = "de"
    await loaded.runtime_data.coordinator.async_refresh()
    await _settle(hass)
    ret = hass.states.get(SENSOR).attributes["returns"][0]
    assert ret["drop_off"] == ["Paketautomat (dhl)", "Any Example store"]
    assert ret["cost"] == "Kostenlose Rücksendung"
