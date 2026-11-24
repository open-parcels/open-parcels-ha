"""Sensors: hub summaries, connection summaries and diagnostics, parcels."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from . import payloads

SUITE_KEYS = [
    "carrier",
    "tracking_number",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "expected_from",
    "expected_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


async def test_hub_summaries(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """The aggregator's sensors, with their attributes."""
    receiving = hass.states.get("sensor.open_parcels_receiving_parcels")
    assert receiving is not None
    assert receiving.state == "2"
    assert receiving.attributes["by_carrier"] == {"DHL": 1, "GLS": 1}
    assert [p["tracking_number"] for p in receiving.attributes["parcels"]] == [
        "TEST0000000001",
        "TEST0000000002",
    ]
    assert hass.states.get("sensor.open_parcels_sending_parcels").state == "1"
    assert hass.states.get("sensor.open_parcels_delivered_parcels").state == "1"
    assert hass.states.get("sensor.open_parcels_sending_delivered_parcels").state == "0"
    assert hass.states.get("sensor.open_parcels_awaiting_pickup").state == "1"
    assert hass.states.get("sensor.open_parcels_en_route_to_pickup_point").state == "0"

    next_delivery = hass.states.get("sensor.open_parcels_next_delivery")
    assert next_delivery.state == "2030-01-02T12:00:00+00:00"
    assert next_delivery.attributes["tracking_number"] == "TEST0000000001"
    assert next_delivery.attributes["by_carrier"] == {
        "DHL": "2030-01-02T12:00:00+00:00"
    }
    assert next_delivery.attributes["parcel"]["tracking_number"] == "TEST0000000001"

    assert hass.states.get("sensor.open_parcels_last_successful_update").state not in (
        "unknown",
        "unavailable",
    )


async def test_connection_sensors(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """Each connection shows its own lists and its state."""
    assert hass.states.get("sensor.dhl_germany_abc123_receiving_parcels").state == "1"
    assert hass.states.get("sensor.dhl_germany_abc123_delivered_parcels").state == "1"
    assert hass.states.get("sensor.gls_99999_awaiting_pickup").state == "1"
    assert hass.states.get("sensor.gls_99999_next_delivery").state == "unknown"
    assert hass.states.get("sensor.gls_99999_en_route_to_pickup_point").state == "0"
    assert hass.states.get("sensor.dhl_germany_abc123_sending_parcels").state == "0"
    assert (
        hass.states.get("sensor.dhl_germany_abc123_sending_delivered_parcels").state
        == "0"
    )

    state = hass.states.get("sensor.gls_99999_connection_state")
    assert state.state == "needs_sign_in"
    assert state.attributes["detail"] == "Sign in again"
    assert state.attributes["provider"] == "gls"
    assert (
        hass.states.get("sensor.dhl_germany_abc123_last_successful_update").state
        == "2030-01-02T09:00:00+00:00"
    )


async def test_parcel_sensor_has_suite_shape(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    """State is the status; the suite keys come first, in the suite's order."""
    state = hass.states.get("device_tracker.dhl_germany_abc123_parcel_test0000000001")
    assert state.state == "out_for_delivery"
    keys = [k for k in state.attributes if k in SUITE_KEYS]
    assert keys == SUITE_KEYS
    assert state.attributes["carrier"] == "DHL"
    assert state.attributes["carrier_id"] == "dhl"
    assert state.attributes["history"] is None
    assert state.attributes["raw"] is None
    assert state.attributes["id"] == payloads.PARCEL_A
    # Courier coordinates never reach the parcel's attributes.
    assert "latitude" not in state.attributes["live"]
    assert state.attributes["live"]["stops_away"] == 3


async def test_unknown_connection_state_is_unknown(
    hass: HomeAssistant, loaded: MockConfigEntry, client
) -> None:
    """A state this version does not know shows as unknown, not an error."""
    connections = [dict(s) for s in payloads.CONNECTIONS]
    connections[0]["state"] = "something_new"
    client.async_get_connections.side_effect = lambda: connections
    await loaded.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.dhl_germany_abc123_connection_state").state == "unknown"
    )


async def test_history_option(
    hass: HomeAssistant, entry: MockConfigEntry, client
) -> None:
    """With the option on, history is sorted oldest first."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"include_history": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client.async_get_parcels.assert_awaited_with(include_history=True, limit=1000)
    state = hass.states.get("device_tracker.dhl_germany_abc123_parcel_test0000000001")
    assert [h["raw_status"] for h in state.attributes["history"]] == [
        "Sorted",
        "With the courier",
    ]


async def test_delivered_filter_by_count(
    hass: HomeAssistant, entry: MockConfigEntry, client
) -> None:
    """The parcels filter keeps the N most recent deliveries; others lose their entity."""
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={"delivered_filter_type": "parcels", "delivered_filter_amount": 1},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.open_parcels_delivered_parcels").state == "1"

    hass.config_entries.async_update_entry(
        entry, options={"delivered_filter_type": "days", "delivered_filter_amount": 1}
    )
    parcels = payloads.parcels()
    parcels[2]["delivered_at"] = "2000-01-01T00:00:00.000Z"
    client.async_get_parcels.side_effect = lambda **_: parcels
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.open_parcels_delivered_parcels").state == "0"
    assert (
        hass.states.get("device_tracker.dhl_germany_abc123_parcel_test0000000003")
        is None
    )
