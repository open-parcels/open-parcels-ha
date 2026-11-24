"""Connections added, renamed or idle on the server reach Home Assistant without a resync.

Regressions from a live end-to-end run. Every id and label is made up.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.const import DOMAIN
from custom_components.open_parcels.device import find_device

from . import payloads
from .conftest import FakeStream

NEW_CONNECTION = "33333333-3333-4333-8333-333333333333"


def _device(hass: HomeAssistant, entry: MockConfigEntry, connection_id: str):
    return find_device(
        hass, entry.entry_id, (DOMAIN, f"{entry.entry_id}_connection_{connection_id}")
    )


def _server_lists_new_connection(client: MagicMock) -> None:
    """The server's REST list holds the new connection too, as it would after creating it."""
    client.async_get_connections.side_effect = lambda: [
        *(dict(s) for s in payloads.CONNECTIONS),
        {
            "id": NEW_CONNECTION,
            "provider": "gls",
            "kind": "tracking",
            "label": "Made-up tracking",
            "state": "ok",
            "state_detail": "Not run yet.",
        },
    ]


def _snapshot(connection_id: str, provider: str, label: str, **extra) -> str:
    return json.dumps(
        {
            "connection": {
                "id": connection_id,
                "provider": provider,
                "label": label,
                "kind": "tracking",
            },
            "state": "ok",
            "detail": None,
            **extra,
        }
    )


async def test_added_connection_gets_its_device_at_once(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """connection.added creates the device before any parcel or resync."""
    assert _device(hass, loaded, NEW_CONNECTION) is None
    _server_lists_new_connection(client)
    stream.push(
        "connection.added",
        _snapshot(NEW_CONNECTION, "gls", "Made-up tracking", detail="Not run yet."),
        "60",
    )
    await hass.async_block_till_done()
    device = _device(hass, loaded, NEW_CONNECTION)
    assert device is not None
    assert device.name == "GLS Made-up tracking"
    # The resync it asks for brings the fields the event lacks.
    assert client.async_get_connections.await_count >= 2


async def test_run_of_an_unknown_connection_adds_its_device(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """A connection.run_finished for a connection not seen yet is the fallback."""
    _server_lists_new_connection(client)
    stream.push(
        "connection.run_finished",
        _snapshot(NEW_CONNECTION, "gls", "Made-up tracking", ok=True),
    )
    await hass.async_block_till_done()
    assert _device(hass, loaded, NEW_CONNECTION) is not None


async def test_rename_reaches_the_device_but_keeps_a_user_name(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream, client: MagicMock
) -> None:
    """connection.updated renames the device; a name set in Home Assistant wins."""
    gls = payloads.CONNECTION_GLS
    client.async_get_connections.side_effect = lambda: [
        {**s, "label": "GLS renamed"} if s["id"] == gls else dict(s)
        for s in payloads.CONNECTIONS
    ]
    stream.push("connection.updated", _snapshot(gls, "gls", "GLS renamed"), "61")
    await hass.async_block_till_done()
    device = _device(hass, loaded, gls)
    assert device.name == "GLS renamed"

    dr.async_get(hass).async_update_device(device.id, name_by_user="My parcels")
    client.async_get_connections.side_effect = lambda: [
        {**s, "label": "GLS again"} if s["id"] == gls else dict(s)
        for s in payloads.CONNECTIONS
    ]
    stream.push("connection.updated", _snapshot(gls, "gls", "GLS again"), "62")
    await hass.async_block_till_done()
    device = _device(hass, loaded, gls)
    assert device.name == "GLS again"
    assert device.name_by_user == "My parcels"


async def test_idle_connection_shows_idle_with_its_reason(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The server's "idle" state shows as idle, the detail says why."""
    reason = "Public tracking off: nothing to look up."
    stream.push(
        "connection.run_finished",
        _snapshot(
            payloads.CONNECTION_GLS,
            "gls",
            "GLS (99999)",
            state="idle",
            detail=reason,
            ok=True,
        ),
        "63",
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.gls_99999_connection_state")
    assert state.state == "idle"
    assert state.attributes["detail"] == reason

    # The next real success is "ok" again.
    stream.push(
        "connection.run_finished",
        _snapshot(payloads.CONNECTION_GLS, "gls", "GLS (99999)", ok=True),
        "64",
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.gls_99999_connection_state").state == "ok"
