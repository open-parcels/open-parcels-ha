"""Parcel trackers: one per open parcel, placed by the courier or the latest scan."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.const import DOMAIN

from . import payloads
from .conftest import FakeStream

LIVE = "device_tracker.dhl_germany_abc123_parcel_test0000000001"
PICKUP = "device_tracker.gls_99999_parcel_test0000000002"


async def test_courier_position_then_none(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The status is the state; the courier places it; positions are never recorded."""
    state = hass.states.get(LIVE)
    assert state is not None
    assert state.state == "out_for_delivery"
    assert state.attributes["latitude"] == 50.0
    assert state.attributes["stops_away"] == 3
    assert state.attributes["gps_accuracy"] == 15
    assert state.attributes["position_source"] == "courier"
    assert state.attributes["barcode"] == "TEST0000000001"

    stream.push(
        "parcel.live_position",
        json.dumps(
            {
                "parcel_id": payloads.PARCEL_A,
                "latitude": 50.5,
                "longitude": 8.5,
                "accuracy": None,
                "positionAt": "2030-01-02T09:31:00.000Z",
                "stops_away": 2,
            }
        ),
    )
    stream.push(
        "parcel.live_position",
        json.dumps({"parcel_id": payloads.PARCEL_B, "latitude": 1, "longitude": 2}),
    )
    await hass.async_block_till_done()
    state = hass.states.get(LIVE)
    assert state.attributes["latitude"] == 50.5
    assert state.attributes["gps_accuracy"] == 0
    assert state.attributes["position_at"] == "2030-01-02T09:31:00.000Z"
    assert state.attributes["stops_away"] == 2

    entity = hass.data["device_tracker"].get_entity(LIVE)
    assert {"latitude", "longitude", "gps_accuracy", "history"} <= set(
        entity._Entity__combined_unrecorded_attributes
    )
    # Names, addresses and zone presence stay out of history too (HA-024).
    assert {"in_zones", "sender", "receiver", "url", "display_name"} <= set(
        entity._Entity__combined_unrecorded_attributes
    )
    for entity_id, keys in (
        ("sensor.open_parcels_next_delivery", {"barcode", "sender", "receiver"}),
        ("calendar.open_parcels_deliveries", {"description", "location"}),
    ):
        domain = entity_id.split(".")[0]
        other = hass.data[domain].get_entity(entity_id)
        assert keys <= set(other._Entity__combined_unrecorded_attributes)

    # Without the courier and without a placed scan, the tracker stays, off the map.
    stream.push(
        "parcel.live_position",
        json.dumps({"parcel_id": payloads.PARCEL_A, "latitude": None}),
    )
    await hass.async_block_till_done()
    state = hass.states.get(LIVE)
    assert state.state == "out_for_delivery"
    assert "latitude" not in state.attributes
    assert state.attributes["position_source"] is None


async def test_scan_position_and_finished_parcels(
    hass: HomeAssistant, entry: MockConfigEntry, client
) -> None:
    """A placed scan positions a parcel; finished and unknown parcels get no tracker."""
    parcels = payloads.parcels()
    parcels[1]["location"] = {
        "latitude": 51.0,
        "longitude": 7.0,
        "source": "event",
        "precision": "exact",
        "name": "Example hub",
        "address": None,
        "at": "2030-01-02T08:00:00.000Z",
    }
    parcels[3]["status"] = "unknown"
    client.async_get_parcels.side_effect = lambda **_: parcels
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(PICKUP)
    assert state.state == "at_pickup_point"
    assert state.attributes["latitude"] == 51.0
    assert state.attributes["position_source"] == "scan"
    assert state.attributes["position_at"] == "2030-01-02T08:00:00.000Z"
    assert hass.states.get("device_tracker.open_parcels_parcel_test0000000004") is None


async def test_legacy_parcel_entities_are_removed(
    hass: HomeAssistant, entry: MockConfigEntry, client
) -> None:
    """Status sensors and courier trackers from earlier versions are dropped."""
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_sensor = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_parcel_{payloads.PARCEL_A}",
        config_entry=entry,
    )
    old_courier = registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        f"{entry.entry_id}_courier_{payloads.PARCEL_A}",
        config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old_sensor.entity_id) is None
    assert registry.async_get(old_courier.entity_id) is None
    assert hass.states.get(LIVE) is not None


async def test_parcel_tracker_is_not_diagnostic(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    """A parcel tracker is a main entity, not filed under diagnostics (HA-008)."""
    entry = er.async_get(hass).async_get(LIVE)
    assert entry is not None
    assert entry.entity_category is None


async def test_position_time_in_snake_case(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The courier message's `position_at` (newer servers) is read like `positionAt`."""
    stream.push(
        "parcel.live_position",
        json.dumps(
            {
                "parcel_id": payloads.PARCEL_A,
                "latitude": 50.25,
                "longitude": 8.25,
                "position_at": "2030-01-02T09:45:00.000Z",
            }
        ),
    )
    await hass.async_block_till_done()
    state = hass.states.get(LIVE)
    assert state.attributes["latitude"] == 50.25
    assert state.attributes["position_at"] == "2030-01-02T09:45:00.000Z"


async def test_courier_stays_at_the_next_stop_until_delivered(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The courier is not hidden one stop away; the delivery ends the tracker."""
    live = {
        "state": "next_stop",
        "stops_away": 1,
        "latitude": 50.75,
        "longitude": 8.75,
        "accuracy": 10,
        "position_at": "2030-01-02T09:50:00.000Z",
        "message": None,
        "updated_at": "2030-01-02T09:50:00.000Z",
    }
    parcel = payloads.parcels()[0]
    parcel.update(live=live, updated_at="2030-01-02T09:50:00.000Z")
    stream.push(
        "parcel.live_updated",
        json.dumps({"parcel": parcel, "old_live_state": "en_route"}),
        "300",
    )
    await hass.async_block_till_done()
    state = hass.states.get(LIVE)
    assert state.attributes["position_source"] == "courier"
    assert state.attributes["latitude"] == 50.0

    parcel.update(
        status="delivered",
        delivered=True,
        delivered_at="2030-01-02T10:00:00.000Z",
        live={**live, "state": "delivered"},
        updated_at="2030-01-02T10:00:00.000Z",
    )
    stream.push("parcel.delivered", json.dumps({"parcel": parcel}), "301")
    await hass.async_block_till_done()
    assert hass.states.get(LIVE) is None
    assert payloads.PARCEL_A not in loaded.runtime_data.coordinator.positions
