"""The server's health, its backups and new orders (server 2.0 status and events)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.api import (
    OpenParcelsApiError,
    OpenParcelsScopeError,
)

from . import payloads
from .conftest import FakeStream, events

HEALTH = "binary_sensor.open_parcels_server_health"
LAST_BACKUP = "sensor.open_parcels_last_backup"
BACK_UP = "button.open_parcels_back_up_now"


async def test_health_follows_the_status(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock, stream: FakeStream
) -> None:
    """Off while `ok`; a connection's change re-reads the status, and degraded is on."""
    state = hass.states.get(HEALTH)
    assert state.state == "off"
    assert state.attributes["checks"]["database"] == "ok"
    assert state.attributes["version"] == "2.0.0"

    client.async_get_status.side_effect = lambda: payloads.status("degraded")
    stream.push(
        "connection.state_changed",
        json.dumps(
            {
                "connection": {"id": payloads.CONNECTION_DHL},
                "old_state": "ok",
                "new_state": "error",
                "detail": "made up",
            }
        ),
        "5",
    )
    await hass.async_block_till_done()
    coordinator = loaded.runtime_data.coordinator
    await coordinator.async_refresh_status()
    await hass.async_block_till_done()
    assert hass.states.get(HEALTH).state == "on"


async def test_last_backup_and_failed_backup_event(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The newest backup with its file; a failed one updates it and fires an event."""
    state = hass.states.get(LAST_BACKUP)
    assert state.state == "2030-01-02T03:00:00+00:00"
    assert state.attributes["ok"] is True
    assert state.attributes["file"] == "open-parcels-20300102-030000.db"

    fired = events(hass, "open_parcels_backup_failed")
    failure = {
        "ok": False,
        "reason": "scheduled",
        "at": "2030-01-03T03:00:00.000Z",
        "file": None,
        "bytes": None,
        "error": "disk full",
        "pruned": 0,
    }
    stream.push("backup.finished", json.dumps(failure), "6")
    stream.push("backup.finished", json.dumps(failure), "6")
    await hass.async_block_till_done()
    state = hass.states.get(LAST_BACKUP)
    assert state.state == "2030-01-03T03:00:00+00:00"
    assert state.attributes["ok"] is False
    assert state.attributes["error"] == "disk full"
    assert len(fired) == 1
    assert fired[0].data["error"] == "disk full"
    assert fired[0].data["hub_device_id"] is not None


async def test_back_up_button(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """With an admin token the hub gets a button that backs the server up."""
    await hass.services.async_call(
        "button", "press", {"entity_id": BACK_UP}, blocking=True
    )
    client.async_back_up.assert_awaited_once()

    client.async_back_up.side_effect = OpenParcelsApiError(
        500, "backup_failed", "The backup failed: disk full."
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": BACK_UP}, blocking=True
        )


async def test_reader_token_has_no_back_up_button(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """Without the admin scope: no button, and the last backup comes from the status."""
    client.async_get_backups.side_effect = OpenParcelsScopeError(403, "forbidden", None)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(BACK_UP) is None
    state = hass.states.get(LAST_BACKUP)
    assert state.state == "2030-01-02T03:00:00+00:00"
    assert state.attributes["ok"] is True
    assert state.attributes["file"] is None


async def test_order_created_fires_once(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """A new order goes on the bus with its total, once, without its priced items."""
    fired = events(hass, "open_parcels_order_created")
    order = {
        **payloads.orders()[0],
        "id": "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f",
        "order_number": "EX-2",
        "status": "ordered",
        "items": [{"title": "Example cable", "price": 3.0}],
    }
    stream.push("order.created", json.dumps(order), "7")
    stream.push("order.created", json.dumps(order), "7")
    await hass.async_block_till_done()
    assert len(fired) == 1
    data = fired[0].data
    assert data["order_number"] == "EX-2"
    assert data["total"] == {"amount": 15.0, "currency": "EUR", "partial": False}
    assert data["item_count"] == 1
    assert "items" not in data
    coordinator = loaded.runtime_data.coordinator
    assert order["id"] in coordinator.data.orders
