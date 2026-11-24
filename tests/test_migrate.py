"""Version 1 entries get the glossary's ids; entity ids and devices stay."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.const import CONF_API_TOKEN, DOMAIN
from custom_components.open_parcels.migrate import v2_unique_suffix

from . import payloads
from .conftest import TOKEN, URL


def test_v2_unique_suffix() -> None:
    """Connection prefix, connection-state key and directions are renamed."""
    assert v2_unique_suffix("incoming") == "receiving"
    assert v2_unique_suffix("outgoing_delivered_parcels") == (
        "sending_delivered_parcels"
    )
    assert (
        v2_unique_suffix("source_abc_source_state") == "connection_abc_connection_state"
    )
    assert v2_unique_suffix("source_abc_outgoing_delivered") == (
        "connection_abc_sending_delivered"
    )
    assert v2_unique_suffix("source_abc_problem") == "connection_abc_problem"
    assert v2_unique_suffix("parcel_123") == "parcel_123"
    assert v2_unique_suffix("deliveries") == "deliveries"


async def test_v1_entry_is_migrated_in_place(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Unique ids and device identifiers move; entity ids are kept."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=URL,
        data={CONF_URL: URL, CONF_API_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    gls = payloads.CONNECTION_GLS
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_source_{gls}")},
    )
    entities = er.async_get(hass)
    incoming = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_incoming_parcels",
        config_entry=entry,
        suggested_object_id="open_parcels_incoming_parcels",
    )
    state = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_source_{gls}_source_state",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="gls_source_state",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    moved = entities.async_get(incoming.entity_id)
    assert moved is not None
    assert moved.unique_id == f"{entry.entry_id}_receiving_parcels"
    moved_state = entities.async_get(state.entity_id)
    assert moved_state is not None
    assert (
        moved_state.unique_id == f"{entry.entry_id}_connection_{gls}_connection_state"
    )
    migrated = devices.async_get(device.id)
    assert migrated is not None
    assert migrated.identifiers == {(DOMAIN, f"{entry.entry_id}_connection_{gls}")}


async def test_newer_entry_is_refused(hass: HomeAssistant, client: MagicMock) -> None:
    """An entry written by a newer release is not set up."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data={CONF_URL: URL, CONF_API_TOKEN: TOKEN}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
