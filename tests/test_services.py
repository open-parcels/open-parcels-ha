"""Services: track, archive, refresh."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.open_parcels.api import (
    OpenParcelsApiError,
    OpenParcelsConnectionError,
    OpenParcelsNotFoundError,
)
from custom_components.open_parcels.const import CONF_API_TOKEN, DOMAIN

from . import payloads
from .conftest import TOKEN


async def test_track_parcel(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """Tracking posts the code and returns the new parcel's id."""
    response = await hass.services.async_call(
        DOMAIN,
        "track_parcel",
        {"tracking_number": " TEST0000000009 ", "label": "Shoes"},
        blocking=True,
        return_response=True,
    )
    client.async_track_parcel.assert_awaited_once_with(
        "TEST0000000009", carrier=None, label="Shoes", direction="receiving"
    )
    assert response == {"parcel_id": payloads.PARCEL_A, "carrier": "dhl"}


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (
            OpenParcelsApiError(409, "duplicate", "Already tracked"),
            ServiceValidationError,
        ),
        (OpenParcelsConnectionError("down"), HomeAssistantError),
    ],
)
async def test_track_parcel_errors(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    client: MagicMock,
    error: Exception,
    raised: type[Exception],
) -> None:
    """A refused request is the user's to fix; a network error is not."""
    client.async_track_parcel.side_effect = error
    with pytest.raises(raised):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {"tracking_number": "TEST0000000009"}, blocking=True
        )


async def test_archive_by_tracking_number(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """Archive finds the parcel by code (case-insensitive)."""
    await hass.services.async_call(
        DOMAIN, "archive_parcel", {"tracking_number": "test0000000002"}, blocking=True
    )
    client.async_archive_parcel.assert_awaited_once_with(payloads.PARCEL_B)


async def test_archive_errors(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """Archive needs a parcel that exists, and reports server failures."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "archive_parcel", {}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "archive_parcel",
            {"parcel_id": "99999999-0000-4000-8000-000000000000"},
            blocking=True,
        )
    # Malformed input fails in the schema, before any request (HA-021).
    for bad in ({"parcel_id": "nope"}, {"tracking_number": " x "}):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "archive_parcel", bad, blocking=True)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {"tracking_number": "TEST0000000009", "carrier": "x" * 65},
            blocking=True,
        )
    client.async_archive_parcel.side_effect = OpenParcelsConnectionError("down")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "archive_parcel", {"parcel_id": payloads.PARCEL_B}, blocking=True
        )


async def test_archive_of_a_parcel_the_server_no_longer_shows(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """A 404 means the parcel is gone (deleted, or its connection disabled): it is dropped."""
    client.async_archive_parcel.side_effect = OpenParcelsNotFoundError(
        404, "not_found", None
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "archive_parcel", {"parcel_id": payloads.PARCEL_B}, blocking=True
        )
    await hass.async_block_till_done()
    assert payloads.PARCEL_B not in loaded.runtime_data.coordinator.data.parcels
    assert hass.states.get("device_tracker.gls_99999_parcel_test0000000002") is None


async def test_refresh(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """Refresh resyncs; with a parcel it first runs that parcel's connections."""
    before = client.async_get_parcels.await_count
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()
    assert client.async_get_parcels.await_count > before
    client.async_sync_connection.assert_not_awaited()

    await hass.services.async_call(
        DOMAIN, "refresh", {"parcel_id": payloads.PARCEL_A}, blocking=True
    )
    client.async_sync_connection.assert_awaited_once_with(payloads.CONNECTION_DHL)

    client.async_sync_connection.side_effect = OpenParcelsConnectionError("down")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "refresh", {"parcel_id": payloads.PARCEL_A}, blocking=True
        )

    # A connection backing off: the server's wait message, as on the button (HA-021).
    client.async_sync_connection.side_effect = OpenParcelsApiError(
        429, "connection_backing_off", "Try again in 5 minutes"
    )
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN, "refresh", {"parcel_id": payloads.PARCEL_A}, blocking=True
        )
    assert raised.value.translation_key == "sync_backing_off"


async def test_entry_selection(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """With two servers the caller must choose; a named entry is used."""
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://other.example.test",
        data={CONF_URL: "http://other.example.test", CONF_API_TOKEN: TOKEN},
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "refresh", {"config_entry_id": other.entry_id}, blocking=True
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "refresh", {"config_entry_id": "missing"}, blocking=True
        )
