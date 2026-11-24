"""Setup, devices, unload."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels import async_remove_config_entry_device
from custom_components.open_parcels.api import (
    OpenParcelsAuthError,
    OpenParcelsConnectionError,
)
from custom_components.open_parcels.const import DOMAIN
from custom_components.open_parcels.device import find_device

from . import payloads


async def test_setup_creates_hub_and_source_devices(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    """A hub device plus one device per source, linked to the hub."""
    assert loaded.state is ConfigEntryState.LOADED
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    assert hub is not None
    assert hub.name == "Open Parcels"
    dhl = find_device(
        hass,
        loaded.entry_id,
        (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_DHL}"),
    )
    gls = find_device(
        hass,
        loaded.entry_id,
        (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_GLS}"),
    )
    assert dhl is not None and gls is not None
    assert dhl.name == "DHL Germany (abc123)"
    assert dhl.manufacturer == "DHL"
    assert dhl.model == "account"
    # The label already starts with the carrier, so it is not repeated.
    assert gls.name == "GLS (99999)"
    assert dhl.via_device_id == hub.id
    assert gls.via_device_id == hub.id


async def test_parcel_entities_land_on_their_source_device(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    """Parcels belong to their primary (or hand-off) source; sourceless to the hub."""
    entities = er.async_get(hass)

    def device_name(entity_id: str) -> str | None:
        entity = entities.async_get(entity_id)
        assert entity is not None, entity_id
        device = dr.async_get(hass).async_get(entity.device_id or "")
        return device.name if device else None

    assert device_name("device_tracker.dhl_germany_abc123_parcel_test0000000001") == (
        "DHL Germany (abc123)"
    )
    assert (
        device_name("device_tracker.gls_99999_parcel_test0000000002") == "GLS (99999)"
    )
    assert (
        device_name("device_tracker.open_parcels_parcel_test0000000004")
        == "Open Parcels"
    )
    # A delivered parcel has no entity of its own; it is listed by the sensors.
    assert (
        entities.async_get("device_tracker.dhl_germany_abc123_parcel_test0000000003")
        is None
    )
    delivered = hass.states.get("sensor.dhl_germany_abc123_delivered_parcels")
    assert delivered.attributes["parcel_ids"] == [payloads.PARCEL_C]


async def test_setup_retries_when_server_unreachable(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """A connection error makes the entry retry."""
    client.async_get_sources.side_effect = OpenParcelsConnectionError("down")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    # A source device of an entry that is not loaded may go; the hub may not (HA-016).
    devices = dr.async_get(hass)
    hub = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    source = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_source_{payloads.SOURCE_DHL}")},
    )
    assert not await async_remove_config_entry_device(hass, entry, hub)
    assert await async_remove_config_entry_device(hass, entry, source)


async def test_setup_refused_token_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    """A 401 fails setup with a reauth flow."""
    client.async_get_sources.side_effect = OpenParcelsAuthError(401, "unauthorized", "")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_unload(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """The entry unloads cleanly; services stay registered."""
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.NOT_LOADED
    assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_removed_source_drops_its_device(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """A source that disappears from the server loses its device and entities."""
    client.async_get_sources.side_effect = lambda: [dict(payloads.SOURCES[0])]
    coordinator = loaded.runtime_data.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        find_device(
            hass,
            loaded.entry_id,
            (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_GLS}"),
        )
        is None
    )
    assert hass.states.get("sensor.gls_99999_incoming_parcels") is None


async def test_remove_config_entry_device(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    """The hub and live sources cannot be removed by hand; stale ones can."""
    hub = find_device(hass, loaded.entry_id, (DOMAIN, loaded.entry_id))
    dhl = find_device(
        hass,
        loaded.entry_id,
        (DOMAIN, f"{loaded.entry_id}_source_{payloads.SOURCE_DHL}"),
    )
    assert hub is not None and dhl is not None
    assert not await async_remove_config_entry_device(hass, loaded, hub)
    assert not await async_remove_config_entry_device(hass, loaded, dhl)
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=loaded.entry_id,
        identifiers={(DOMAIN, f"{loaded.entry_id}_source_gone")},
    )
    assert await async_remove_config_entry_device(hass, loaded, stale)
