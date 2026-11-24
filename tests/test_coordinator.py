"""Event-stream messages: state updates and Home Assistant bus events."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.open_parcels.api import OpenParcelsConnectionError, SseMessage
from custom_components.open_parcels.const import DOMAIN
from custom_components.open_parcels.coordinator import OpenParcelsCoordinator
from custom_components.open_parcels.device import find_device
from custom_components.open_parcels.merge import position_from_live
from custom_components.open_parcels.parcels import to_suite_parcel

from . import payloads
from .conftest import FakeStream, events


def _parcel(index: int, **changes) -> dict:
    parcel = payloads.parcels()[index]
    parcel.update(changes)
    return parcel


async def test_status_change_fires_event_and_updates(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """parcel.status_changed -> open_parcels_parcel_status_changed with device ids."""
    fired = events(hass, f"{DOMAIN}_parcel_status_changed")
    parcel = _parcel(1, status="returning")
    stream.push(
        "parcel.status_changed",
        json.dumps(
            {
                "parcel": parcel,
                "old_status": "at_pickup_point",
                "new_status": "returning",
            }
        ),
        "41",
    )
    await hass.async_block_till_done()

    assert (
        hass.states.get("device_tracker.gls_99999_parcel_test0000000002").state
        == "returning"
    )
    assert len(fired) == 1
    data = fired[0].data
    assert data["barcode"] == "TEST0000000002"
    assert data["old_status"] == "at_pickup_point"
    assert data["new_status"] == "returning"
    assert "raw" not in data
    gls = find_device(
        hass,
        loaded.entry_id,
        (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_GLS}"),
    )
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    assert data["device_id"] == gls.id
    assert data["hub_device_id"] == hub.id
    assert loaded.runtime_data.coordinator.last_event_id == "41"


async def test_new_parcel_adds_entity(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """A registered parcel gets its sensor without a resync."""
    fired = events(hass, f"{DOMAIN}_parcel_registered")
    parcel = _parcel(
        1,
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        barcode="TEST0000000005",
        status="registered",
        pickup=False,
    )
    stream.push("parcel.registered", json.dumps({"parcel": parcel}), "42")
    await hass.async_block_till_done()
    assert (
        hass.states.get("device_tracker.gls_99999_parcel_test0000000005").state
        == "registered"
    )
    assert hass.states.get("sensor.open_parcels_incoming_parcels").state == "3"
    assert len(fired) == 1


async def test_removed_parcel_drops_entity(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """parcel.removed takes the parcel and its courier away."""
    fired = events(hass, f"{DOMAIN}_parcel_removed")
    stream.push("parcel.removed", json.dumps({"parcel": _parcel(0)}), "43")
    await hass.async_block_till_done()
    assert (
        hass.states.get("device_tracker.dhl_germany_abc123_parcel_test0000000001")
        is None
    )
    assert (
        hass.states.get("device_tracker.dhl_germany_abc123_parcel_test0000000001")
        is None
    )
    assert len(fired) == 1


async def test_removal_without_sources_goes_to_the_source_device(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """A removal whose payload names no source still fires on the owning source's device."""
    fired = events(hass, f"{DOMAIN}_parcel_removed")
    stream.push("parcel.removed", json.dumps({"parcel": _parcel(1, sources=[])}), "44")
    await hass.async_block_till_done()
    gls = find_device(
        hass,
        loaded.entry_id,
        (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_GLS}"),
    )
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    assert len(fired) == 1
    assert fired[0].data["device_id"] == gls.id
    assert fired[0].data["device_id"] != hub.id


async def test_delivered_and_delivery_time_and_outgoing_events(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """Every server event maps to its suite-style bus event."""
    names = [
        "parcel_delivered",
        "parcel_delivery_time_changed",
        "outgoing_parcel_registered",
        "outgoing_parcel_delivered",
        "parcel_live_updated",
    ]
    fired = {name: events(hass, f"{DOMAIN}_{name}") for name in names}
    delivered = _parcel(
        0,
        status="delivered",
        delivered=True,
        delivered_at="2999-01-01T00:00:00Z",
        live=None,
    )
    stream.push("parcel.delivered", json.dumps({"parcel": delivered}), "50")
    stream.push(
        "parcel.delivery_time_changed",
        json.dumps(
            {
                "parcel": _parcel(1, planned_from="2030-01-05T10:00:00Z"),
                "old_planned_from": None,
                "new_planned_from": "2030-01-05T10:00:00Z",
            }
        ),
        "51",
    )
    stream.push("parcel.outgoing_registered", json.dumps({"parcel": _parcel(3)}), "52")
    stream.push(
        "parcel.outgoing_delivered",
        json.dumps({"parcel": _parcel(3, status="delivered", delivered=True)}),
        "53",
    )
    stream.push(
        "parcel.live_updated",
        json.dumps({"parcel": _parcel(1), "old_live_state": None}),
        "54",
    )
    await hass.async_block_till_done()
    for name in names:
        assert len(fired[name]) == 1, name
    assert fired["parcel_delivery_time_changed"][0].data["new_planned_from"] == (
        "2030-01-05T10:00:00Z"
    )
    assert hass.states.get("sensor.open_parcels_delivered_parcels").state == "2"
    # Live session ended with the delivery: the courier tracker goes away.
    assert (
        hass.states.get("device_tracker.dhl_germany_abc123_parcel_test0000000001")
        is None
    )


async def test_source_state_change(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """source.state_changed updates the source and fires an event."""
    fired = events(hass, f"{DOMAIN}_source_state_changed")
    stream.push(
        "source.state_changed",
        json.dumps(
            {
                "source": {"id": payloads.SOURCE_DHL, "plugin": "dhl", "label": "x"},
                "old_state": "ok",
                "new_state": "error",
                "detail": "Carrier unreachable",
            }
        ),
        "60",
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dhl_germany_abc123_source_state").state == "error"
    assert hass.states.get("binary_sensor.dhl_germany_abc123_problem").state == "on"
    assert fired[0].data["new_state"] == "error"
    assert fired[0].data["device_id"] is not None


async def test_unknown_source_or_parcel_source_triggers_resync(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """Events naming a source this entry has not seen cause a resync."""
    before = client.async_get_sources.await_count
    stream.push(
        "source.state_changed",
        json.dumps({"source": {"id": "new"}, "old_state": "ok", "new_state": "error"}),
    )
    stream.push(
        "parcel.registered",
        json.dumps(
            {
                "parcel": _parcel(
                    1,
                    id="ffffffff-ffff-4fff-8fff-ffffffffffff",
                    sources=[
                        {
                            "source_id": "unknown-source",
                            "plugin": "gls",
                            "role": "primary",
                            "last_seen_at": "2030-01-01T00:00:00Z",
                        }
                    ],
                )
            }
        ),
    )
    await hass.async_block_till_done()
    assert client.async_get_sources.await_count > before


async def test_ignored_messages(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """Malformed, non-object, unknown and incomplete messages change nothing."""
    coordinator = loaded.runtime_data.coordinator
    stream.push("parcel.registered", "{not json")
    stream.push("parcel.registered", "[1, 2]")
    stream.push("vocab.unknown", json.dumps({"key": "x"}), "70")
    stream.push("parcel.registered", json.dumps({"parcel": {"no": "id"}}))
    stream.push("parcel.live_position", json.dumps({"parcel_id": 5}))
    await hass.async_block_till_done()
    assert coordinator.last_event_id == "70"
    assert hass.states.get("sensor.open_parcels_incoming_parcels").state == "2"


async def test_history_kept_when_event_lacks_it(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """A parcel event without history keeps what the resync loaded."""
    coordinator = loaded.runtime_data.coordinator
    parcel = _parcel(0)
    del parcel["history"]
    stream.push("parcel.status_changed", json.dumps({"parcel": parcel}), "80")
    await hass.async_block_till_done()
    assert len(coordinator.data.parcels[payloads.PARCEL_A]["history"]) == 2


async def test_resync_failure_keeps_entities(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """A failed resync marks entities unavailable but keeps them."""
    client.async_get_parcels.side_effect = OpenParcelsConnectionError("down")
    coordinator = loaded.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.open_parcels_incoming_parcels").state == "unavailable"
    )
    # The event stream sensor reports the stream, not the resync.
    assert hass.states.get("binary_sensor.open_parcels_event_stream").state == "on"


async def test_calendar_failure_is_not_fatal(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """A failing calendar endpoint leaves the cached entries alone."""
    coordinator = loaded.runtime_data.coordinator
    cached = list(coordinator.calendar_entries)
    client.async_get_calendar.side_effect = OpenParcelsConnectionError("down")
    await coordinator.async_refresh_calendar()
    assert coordinator.calendar_entries == cached
    assert coordinator.last_update_success


async def test_views_before_first_refresh(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """Before any data arrives, every view is empty instead of failing."""
    entry.add_to_hass(hass)
    coordinator = OpenParcelsCoordinator(hass, entry, client)
    assert coordinator.suite_parcel(payloads.PARCEL_A) is None
    assert coordinator.tracked_parcel_ids() == set()
    coordinator.async_sync_registry()


async def test_deleted_parcel_drops_entity_and_counts(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """parcel.deleted takes the parcel away at once, no refresh button needed."""
    fired = events(hass, f"{DOMAIN}_parcel_deleted")
    assert hass.states.get("sensor.open_parcels_incoming_parcels").state == "2"
    stream.push("parcel.deleted", json.dumps({"parcel": _parcel(1)}), "90")
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.gls_99999_parcel_test0000000002") is None
    assert hass.states.get("sensor.open_parcels_incoming_parcels").state == "1"
    assert len(fired) == 1


async def test_removed_source_drops_device_and_rehomes_its_parcels(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """source.removed removes the device; its parcels move to the hub."""
    fired = events(hass, f"{DOMAIN}_source_removed")
    dhl_identifier = (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_DHL}")
    dhl = find_device(hass, loaded.entry_id, dhl_identifier)
    assert dhl is not None
    stream.push(
        "source.removed",
        json.dumps(
            {
                "source": {"id": payloads.SOURCE_DHL, "plugin": "dhl", "label": "x"},
                "merged_into": None,
            }
        ),
        "91",
    )
    await hass.async_block_till_done()
    assert find_device(hass, loaded.entry_id, dhl_identifier) is None
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, f"{loaded.entry_id}_parcel_{payloads.PARCEL_A}"
    )
    assert entity_id is not None
    assert er.async_get(hass).async_get(entity_id).device_id == hub.id
    assert fired[0].data["device_id"] == dhl.id
    assert fired[0].data["merged_into"] is None


async def test_resync_removes_what_the_server_no_longer_has(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A periodic resync drops vanished sources (device) and parcels (entity).

    The device is removed through the supported API: no deprecation report.
    """
    client.async_get_sources.side_effect = lambda: [
        dict(s) for s in payloads.SOURCES if s["id"] != payloads.SOURCE_GLS
    ]
    client.async_get_parcels.side_effect = lambda **_: [
        p for p in payloads.parcels() if p["id"] != payloads.PARCEL_B
    ]
    await loaded.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    gls_identifier = (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_GLS}")
    assert find_device(hass, loaded.entry_id, gls_identifier) is None
    assert dr.async_get(hass).async_get_device(identifiers={gls_identifier}) is None
    assert "remove_config_entry_id" not in caplog.text
    assert hass.states.get("device_tracker.gls_99999_parcel_test0000000002") is None
    assert hass.states.get("sensor.open_parcels_incoming_parcels").state == "1"


async def test_source_removed_without_data_or_id_is_ignored(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """A malformed removal changes nothing; a merge into an unseen source resyncs."""
    before = client.async_get_sources.await_count
    stream.push("source.removed", json.dumps({"source": "nope"}))
    await hass.async_block_till_done()
    assert client.async_get_sources.await_count == before
    stream.push(
        "source.removed",
        json.dumps(
            {"source": {"id": payloads.SOURCE_GLS}, "merged_into": "unseen-source"}
        ),
    )
    await hass.async_block_till_done()
    assert client.async_get_sources.await_count > before


def test_carrier_status_comes_from_the_server() -> None:
    """The effective status is `status`; the carrier's own value stays visible."""
    suite = to_suite_parcel(
        _parcel(
            0,
            status="delivered",
            delivered=True,
            manual_status="delivered",
            carrier_status="out_for_delivery",
        ),
        include_history=False,
    )
    assert suite["status"] == "delivered"
    assert suite["delivered"] is True
    assert suite["carrier_status"] == "out_for_delivery"


def test_position_needs_numeric_coordinates_and_an_open_session() -> None:
    """A live block without numbers, or of a finished session, gives no position."""
    assert position_from_live({"state": "en_route", "latitude": "x"}) is None
    assert (
        position_from_live({"state": "delivered", "latitude": 1, "longitude": 2})
        is None
    )
    # The courier stays on the map at the next stop, until the delivery.
    position = position_from_live({"state": "next_stop", "latitude": 1, "longitude": 2})
    assert position is not None
    assert (position.latitude, position.longitude) == (1.0, 2.0)


async def test_replayed_event_never_overwrites_newer_state(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """A replayed parcel event older than the REST state fires but keeps the state."""
    fired = events(hass, f"{DOMAIN}_parcel_status_changed")
    coordinator = loaded.runtime_data.coordinator
    stored = coordinator.data.parcels[_parcel(1)["id"]]
    stale = _parcel(1, status="registered", updated_at="2000-01-01T00:00:00.000Z")
    stream.push(
        "parcel.status_changed",
        json.dumps({"parcel": stale, "old_status": "x", "new_status": "registered"}),
        "50",
    )
    await hass.async_block_till_done()
    assert len(fired) == 1
    assert coordinator.data.parcels[stale["id"]] is stored
    assert stored["status"] != "registered"


async def test_refresh_asked_during_cooldown_survives_the_event(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refresh an event queues inside the debouncer's cooldown still runs (HA-002)."""
    coordinator = loaded.runtime_data.coordinator
    # A service call refreshes first: the debouncer's cooldown starts.
    await coordinator.async_request_refresh()
    await hass.async_block_till_done()
    before = client.async_get_sources.await_count
    # Within the cooldown, an event names a source this entry has not seen.
    stream.push(
        "source.state_changed",
        json.dumps({"source": {"id": "new"}, "old_state": "ok", "new_state": "error"}),
        "61",
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert client.async_get_sources.await_count == before + 1


async def test_events_do_not_postpone_the_periodic_resync(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """Events leave the slow resync's timer alone, so it runs on time (HA-003).

    The handle is compared because the test clock cannot move the event loop's
    own time, which the timer is scheduled on.
    """
    coordinator = loaded.runtime_data.coordinator
    await hass.async_block_till_done()
    scheduled = coordinator._unsub_refresh
    assert scheduled is not None
    for seq in range(3):
        stream.push(
            "parcel.status_changed",
            json.dumps({"parcel": _parcel(1, status="returning")}),
            str(100 + seq),
        )
        await hass.async_block_till_done()
    assert coordinator._unsub_refresh is scheduled


async def test_truncated_message_reloads(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A message cut for its size is not applied; a reload repairs it (HA-032)."""
    coordinator = loaded.runtime_data.coordinator
    await hass.async_block_till_done()
    before = client.async_get_sources.await_count
    stream.queue.put_nowait(
        SseMessage(event="parcel.status_changed", data="", id="80", truncated=True)
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.last_event_id == "80"
    assert client.async_get_sources.await_count == before + 1
