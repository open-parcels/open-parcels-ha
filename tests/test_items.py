"""Item pictures, names from the server and the optional order sensors."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.open_parcels import picture as picture_module
from custom_components.open_parcels.api import OpenParcelsConnectionError
from custom_components.open_parcels.const import CONF_ORDER_ENTITIES
from custom_components.open_parcels.parcels import order_key

from . import payloads
from .conftest import FakeStream

LIVE = "device_tracker.dhl_germany_abc123_parcel_test0000000001"
PICKUP = "device_tracker.gls_99999_parcel_test0000000002"

# Made up: a shop order with two lines, one in parcel A, one not placed yet.
ORDER = {
    "shop": "Example shop",
    "order_number": "EX-1",
    "items": [
        {
            "title": "Example lamp",
            "image_url": "/img/abc.def",
            "url": None,
            "quantity": 1,
            "price": 10.0,
            "currency": "EUR",
            "parcel_id": payloads.PARCEL_A,
        },
        {
            "title": "Example voucher",
            "image_url": None,
            "url": None,
            "quantity": 1,
            "price": 5.0,
            "currency": "EUR",
            "parcel_id": None,
        },
    ],
}


def _with_items(parcels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parcels[0]["items"] = [dict(ORDER["items"][0])]
    parcels[0]["order"] = dict(ORDER)
    parcels[0]["display_name"] = "Example lamp"
    parcels[1]["display_name"] = parcels[1]["barcode"]
    return parcels


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, client, **options: Any
) -> None:
    parcels = _with_items(payloads.parcels())
    client.async_get_parcels.side_effect = lambda **_: parcels
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_names_and_signed_pictures(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A display name other than the code names the entity; items give a picture."""
    await _setup(hass, entry, client)
    lamp = hass.states.get(LIVE)
    assert lamp.attributes["friendly_name"].endswith("Example lamp")
    # Named by its code, the parcel keeps the translated "Parcel <code>".
    assert (
        hass.states.get(PICKUP)
        .attributes["friendly_name"]
        .endswith("Parcel TEST0000000002")
    )
    assert "entity_picture" not in hass.states.get(PICKUP).attributes

    picture = lamp.attributes["entity_picture"]
    assert picture.startswith(f"/api/open_parcels/picture/{entry.entry_id}/")
    client.async_get_parcel_picture = AsyncMock(return_value=b"\x89PNG made up")
    http = await hass_client_no_auth()
    response = await http.get(picture)
    assert response.status == 200
    assert response.content_type == "image/png"
    assert await response.read() == b"\x89PNG made up"
    client.async_get_parcel_picture.assert_awaited_with(payloads.PARCEL_A)

    # A made-up or altered address gets nothing.
    assert (await http.get(picture.replace("sig=", "sig=0"))).status == 403
    other = picture.replace(payloads.PARCEL_A, payloads.PARCEL_B)
    assert (await http.get(other)).status == 403

    client.async_get_parcel_picture = AsyncMock(return_value=None)
    assert (await http.get(picture)).status == 404
    client.async_get_parcel_picture = AsyncMock(
        side_effect=OpenParcelsConnectionError("down")
    )
    assert (await http.get(picture)).status == 502

    # An address lives for its window and the next one, then expires (HA-019).
    client.async_get_parcel_picture = AsyncMock(return_value=b"\x89PNG made up")
    window = picture_module.current_window()
    with patch.object(picture_module, "current_window", return_value=window + 1):
        assert (await http.get(picture)).status == 200
    with patch.object(picture_module, "current_window", return_value=window + 2):
        assert (await http.get(picture)).status == 403
    assert (await http.get(picture.replace("&w=", "&w=x"))).status == 403


async def test_order_sensors_are_optional(
    hass: HomeAssistant, entry: MockConfigEntry, client
) -> None:
    """Off by default; with the option, an open order gets a sensor with its lines."""
    await _setup(hass, entry, client)
    key = order_key("Example shop", "EX-1")
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "sensor", "open_parcels", f"{entry.entry_id}_order_{key}"
        )
        is None
    )

    hass.config_entries.async_update_entry(entry, options={CONF_ORDER_ENTITIES: True})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id(
        "sensor", "open_parcels", f"{entry.entry_id}_order_{key}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.state == "in_progress"
    assert state.attributes["order_number"] == "EX-1"
    assert state.attributes["parcel_count"] == 1
    assert [item["parcel"] for item in state.attributes["items"]] == [
        "TEST0000000001",
        None,
    ]

    hass.config_entries.async_update_entry(entry, options={CONF_ORDER_ENTITIES: False})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(entity_id) is None


async def test_settings_change_reloads(
    hass: HomeAssistant, loaded: MockConfigEntry, client, stream: FakeStream
) -> None:
    """A changed server setting makes the integration read everything again."""
    calls = client.async_get_parcels.await_count
    stream.push("settings.updated", '{"changed": "parcel_label"}')
    await hass.async_block_till_done()
    assert client.async_get_parcels.await_count == calls + 1
