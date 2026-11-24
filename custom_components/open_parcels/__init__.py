"""The Open Parcels integration: parcels from an Open Parcels server."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import OpenParcelsClient
from .const import CONF_API_TOKEN, DOMAIN, PLATFORMS
from .coordinator import OpenParcelsCoordinator
from .device import connection_identifier, hub_device_info, hub_identifier
from .migrate import async_migrate_entry as async_migrate_entry
from .picture import ParcelPictureView
from .services import async_setup_services
from .stream import async_run_stream

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class OpenParcelsRuntimeData:
    """What a loaded entry keeps."""

    client: OpenParcelsClient
    coordinator: OpenParcelsCoordinator


type OpenParcelsConfigEntry = ConfigEntry[OpenParcelsRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and the parcel-picture view once, independent of any entry."""
    async_setup_services(hass)
    hass.http.register_view(ParcelPictureView(hass))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenParcelsConfigEntry) -> bool:
    """Load the server's state, then keep it current through the event stream."""
    client = OpenParcelsClient(
        async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_API_TOKEN]
    )
    coordinator = OpenParcelsCoordinator(hass, entry, client)

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **hub_device_info(entry)
    )
    # Registry sync runs before the platforms' listeners, so stale entities
    # are gone before new ones are added.
    entry.async_on_unload(
        coordinator.async_add_listener(coordinator.async_sync_registry)
    )

    await coordinator.async_config_entry_first_refresh()
    coordinator.async_sync_registry()
    entry.runtime_data = OpenParcelsRuntimeData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Options changes reload the entry (OptionsFlowWithReload).
    entry.async_create_background_task(
        hass, async_run_stream(hass, entry, coordinator), f"{DOMAIN}_event_stream"
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: OpenParcelsConfigEntry
) -> bool:
    """Unload the platforms; the stream task is cancelled with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: OpenParcelsConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a connection device only once the connection is gone.

    An entry that is not loaded (setup retry, disabled) knows no connections, so
    any connection device may go; the next setup creates the live ones again.
    """
    if hub_identifier(entry.entry_id) in device.identifiers:
        return False
    runtime = getattr(entry, "runtime_data", None)
    data = runtime.coordinator.data if runtime is not None else None
    connections = data.connections if data else {}
    return not any(
        connection_identifier(entry.entry_id, connection_id) in device.identifiers
        for connection_id in connections
    )
