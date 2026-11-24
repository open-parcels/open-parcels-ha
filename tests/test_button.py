"""Buttons."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.api import (
    OpenParcelsApiError,
    OpenParcelsConnectionError,
)

from . import payloads


async def test_connection_sync_button(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """The connection button runs the connection; the hub button resyncs."""
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.dhl_germany_abc123_refresh"},
        blocking=True,
    )
    client.async_sync_connection.assert_awaited_once_with(payloads.CONNECTION_DHL)

    before = client.async_get_parcels.await_count
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.open_parcels_refresh"}, blocking=True
    )
    await hass.async_block_till_done()
    assert client.async_get_parcels.await_count > before

    client.async_sync_connection.side_effect = OpenParcelsConnectionError("down")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.dhl_germany_abc123_refresh"},
            blocking=True,
        )


async def test_sync_while_backing_off_shows_the_servers_wait(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """A 429 from a connection in its captcha pause reaches the user with the wait."""
    client.async_sync_connection.side_effect = OpenParcelsApiError(
        429,
        "connection_backing_off",
        "The carrier asked us to wait; this connection runs again in 6 hours.",
    )
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.dhl_germany_abc123_refresh"},
            blocking=True,
        )
    assert raised.value.translation_key == "sync_backing_off"
    assert raised.value.translation_placeholders == {
        "message": "The carrier asked us to wait; this connection runs again in 6 hours."
    }
