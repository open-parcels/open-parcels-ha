"""The stream and the REST reload agree: no lost updates, no hidden data, no repeats.

Regression tests for the audit's HA-001, HA-005, HA-006 and HA-007 findings and for
bus events firing once per change. Every id, code and name is made up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
import json
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.open_parcels import stream as stream_module
from custom_components.open_parcels.api import OpenParcelsConnectionError
from custom_components.open_parcels.const import DOMAIN, RECONNECT_REFRESH_DELAY_SECONDS

from . import payloads
from .conftest import FakeStream, events

PICKUP = "device_tracker.gls_99999_parcel_test0000000002"
LIVE = "device_tracker.dhl_germany_abc123_parcel_test0000000001"
ORDER_ID = "99999999-9999-4999-8999-999999999999"


def _parcel(index: int, **changes: Any) -> dict[str, Any]:
    parcel = payloads.parcels()[index]
    parcel.update(changes)
    return parcel


@pytest.fixture
def no_reconnect_wait() -> Generator[None]:
    """Reconnect at once instead of after the backoff delay."""
    with patch.object(stream_module, "jittered", lambda _delay: 0):
        yield


async def _wait_past_the_reconnect_delay(hass: HomeAssistant) -> None:
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=RECONNECT_REFRESH_DELAY_SECONDS + 1),
    )
    await _settle(hass)


async def _settle(hass: HomeAssistant) -> None:
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0)


async def test_event_during_a_reload_survives_it(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """A change that arrives while the REST requests run is not undone (HA-001)."""
    coordinator = loaded.runtime_data.coordinator
    await hass.async_block_till_done()
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_parcels(**_: Any) -> list[dict[str, Any]]:
        started.set()
        await release.wait()
        return payloads.parcels()  # the snapshot predates the event below

    client.async_get_parcels.side_effect = slow_parcels
    refresh = hass.async_create_task(coordinator.async_refresh())
    await started.wait()
    stream.push(
        "parcel.status_changed",
        json.dumps(
            {
                "parcel": _parcel(
                    1, status="returning", updated_at="2030-01-03T08:00:00.000Z"
                ),
                "old_status": "at_pickup_point",
                "new_status": "returning",
            }
        ),
        "200",
    )
    stream.push("parcel.deleted", json.dumps({"parcel": _parcel(0)}), "201")
    for _ in range(5):
        await asyncio.sleep(0)
    assert coordinator.data.parcels[payloads.PARCEL_B]["status"] == "returning"
    release.set()
    await refresh
    await hass.async_block_till_done()
    assert coordinator.data.parcels[payloads.PARCEL_B]["status"] == "returning"
    assert hass.states.get(PICKUP).state == "returning"
    assert payloads.PARCEL_A not in coordinator.data.parcels
    assert hass.states.get(LIVE) is None


async def test_location_change_moves_the_tracker(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """parcel.location_changed places the parcel at its new scan (HA-005)."""
    location = {
        "latitude": 52.5,
        "longitude": 13.4,
        "source": "event",
        "precision": "city",
        "name": "Example City",
        "address": None,
        "at": "2030-01-03T08:00:00.000Z",
    }
    stream.push(
        "parcel.location_changed",
        json.dumps(
            {
                "parcel": _parcel(
                    1, location=location, updated_at="2030-01-03T08:00:00.000Z"
                )
            }
        ),
        "210",
    )
    await hass.async_block_till_done()
    state = hass.states.get(PICKUP)
    assert state.attributes["latitude"] == 52.5
    assert state.attributes["position_source"] == "scan"


async def test_order_updated_and_deleted(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, stream: FakeStream
) -> None:
    """order.updated patches the parcels' order; order.deleted unlinks it (HA-005)."""
    parcels = payloads.parcels()
    parcels[1]["order"] = {
        "id": ORDER_ID,
        "shop": "Example Store",
        "order_number": "EX-0001",
        "items": [],
    }
    client.async_get_parcels.side_effect = lambda **_: parcels
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    # A refresh just ran, so the one order.updated asks for waits for the cooldown:
    # what follows is the event's own patch.
    await coordinator.async_request_refresh()

    stream.push(
        "order.updated",
        json.dumps(
            {
                "id": ORDER_ID,
                "shop": "Example Store",
                "order_number": "EX-0002",
                "items": [{"title": "Example lamp", "parcel_id": payloads.PARCEL_B}],
                "parcels": [],
            }
        ),
        "220",
    )
    await hass.async_block_till_done()
    order = coordinator.data.parcels[payloads.PARCEL_B]["order"]
    assert order["order_number"] == "EX-0002"
    assert order["items"][0]["title"] == "Example lamp"

    stream.push(
        "order.deleted",
        json.dumps({"id": ORDER_ID, "parcel_ids": [payloads.PARCEL_B]}),
        "221",
    )
    await hass.async_block_till_done()
    assert coordinator.data.parcels[payloads.PARCEL_B]["order"] is None
    assert hass.states.get(PICKUP).attributes["order"] is None


def _state_change(old: str, new: str) -> str:
    return json.dumps(
        {
            "source": {"id": payloads.SOURCE_GLS, "plugin": "gls", "label": "x"},
            "old_state": old,
            "new_state": new,
            "detail": None,
        }
    )


async def test_disabling_and_enabling_a_source(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """A disabled source's parcels go at once and come back on enabling (HA-006)."""
    coordinator = loaded.runtime_data.coordinator
    await hass.async_block_till_done()
    visible = payloads.parcels()

    # The server reports the source disabled and hides its parcels from every list.
    client.async_get_sources.side_effect = lambda: [
        {**s, "state": "disabled"} if s["id"] == payloads.SOURCE_GLS else dict(s)
        for s in payloads.SOURCES
    ]
    client.async_get_parcels.side_effect = lambda **_: [
        p for p in visible if p["id"] != payloads.PARCEL_B
    ]
    before = client.async_get_parcels.await_count
    stream.push(
        "source.state_changed", _state_change("needs_sign_in", "disabled"), "230"
    )
    await hass.async_block_till_done()
    assert payloads.PARCEL_B not in coordinator.data.parcels
    assert hass.states.get(PICKUP) is None
    assert client.async_get_parcels.await_count == before + 1

    # An event for a hidden parcel does not bring it back.
    stream.push(
        "parcel.status_changed",
        json.dumps({"parcel": _parcel(1, status="returning")}),
        "231",
    )
    await hass.async_block_till_done()
    assert payloads.PARCEL_B not in coordinator.data.parcels

    # Enabled again: the reload (after the refresh cooldown) brings the parcel back.
    client.async_get_sources.side_effect = lambda: [dict(s) for s in payloads.SOURCES]
    client.async_get_parcels.side_effect = lambda **_: visible
    stream.push("source.state_changed", _state_change("disabled", "ok"), "232")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert payloads.PARCEL_B in coordinator.data.parcels
    assert hass.states.get(PICKUP) is not None


async def test_reconnect_reloads_once_unless_the_server_resyncs(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    client: MagicMock,
    no_reconnect_wait: None,
) -> None:
    """After a replaying reconnect one reload follows, so a gap cannot stay (HA-007)."""
    coordinator = loaded.runtime_data.coordinator
    stream.push("vocab.unknown", "{}", "300")
    stream.queue.put_nowait(OpenParcelsConnectionError("reset"))
    await _settle(hass)
    assert stream.calls == [None, "300"]
    before = client.async_get_parcels.await_count
    await _wait_past_the_reconnect_delay(hass)
    assert client.async_get_parcels.await_count == before + 1

    # A `resync` right after a reconnect replaces the delayed reload.
    stream.queue.put_nowait(OpenParcelsConnectionError("reset"))
    await _settle(hass)
    before = client.async_get_parcels.await_count
    stream.push("resync", json.dumps({"seq": 400, "reason": "expired"}), "400")
    await _settle(hass)
    await _wait_past_the_reconnect_delay(hass)
    assert client.async_get_parcels.await_count == before + 1
    assert coordinator.last_event_id == "400"


async def test_reconnect_replay_fires_each_change_once(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    no_reconnect_wait: None,
) -> None:
    """A message the server replays after a reconnect does not fire again."""
    fired = events(hass, f"{DOMAIN}_parcel_status_changed")
    message = json.dumps(
        {
            "parcel": _parcel(1, status="returning"),
            "old_status": "at_pickup_point",
            "new_status": "returning",
        }
    )
    stream.push("parcel.status_changed", message, "310")
    stream.queue.put_nowait(OpenParcelsConnectionError("reset"))
    await _settle(hass)
    stream.push("parcel.status_changed", message, "310")
    await _settle(hass)
    assert len(fired) == 1


async def test_stream_and_reload_fire_each_change_once(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The same change sent again (after a reload, under a new id) fires once.

    A later, different change of the same parcel still fires.
    """
    fired = events(hass, f"{DOMAIN}_parcel_status_changed")
    coordinator = loaded.runtime_data.coordinator

    def change(old: str, new: str) -> str:
        return json.dumps(
            {
                "parcel": _parcel(1, status=new),
                "old_status": old,
                "new_status": new,
            }
        )

    stream.push("parcel.status_changed", change("at_pickup_point", "returning"), "320")
    await hass.async_block_till_done()
    await coordinator.async_refresh()
    stream.push("parcel.status_changed", change("at_pickup_point", "returning"), "321")
    await hass.async_block_till_done()
    assert len(fired) == 1
    stream.push("parcel.status_changed", change("returning", "problem"), "322")
    await hass.async_block_till_done()
    assert [event.data["new_status"] for event in fired] == ["returning", "problem"]


async def test_a_reset_server_sequence_is_not_taken_for_a_replay(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """After `resync` with a lower sequence (a reset database), events fire again."""
    fired = events(hass, f"{DOMAIN}_parcel_registered")
    stream.push("vocab.unknown", "{}", "900")
    stream.push("resync", json.dumps({"seq": 5, "reason": "reset"}), "5")
    new = _parcel(
        1,
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        barcode="TEST0000000005",
        status="registered",
    )
    stream.push("parcel.registered", json.dumps({"parcel": new}), "6")
    await hass.async_block_till_done()
    assert len(fired) == 1
