"""Shared entity base and the helper that adds entities as items appear."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION
from .coordinator import OpenParcelsCoordinator
from .device import connection_identifier, hub_device_info


class OpenParcelsEntity(CoordinatorEntity[OpenParcelsCoordinator]):
    """Base for every entity of this integration."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: OpenParcelsCoordinator, unique_suffix: str) -> None:
        """Set the unique id from the entry id and a suffix."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{unique_suffix}"


class OpenParcelsConnectionEntity(OpenParcelsEntity):
    """Base for the entities of one connection, on that connection's device."""

    def __init__(
        self, coordinator: OpenParcelsCoordinator, connection_id: str, key: str
    ) -> None:
        """Key the unique id on the connection and place the entity on its device."""
        super().__init__(coordinator, f"connection_{connection_id}_{key}")
        self._connection_id = connection_id
        self._attr_device_info = device_for_connection(
            coordinator.config_entry, connection_id
        )

    @property
    def _connection(self) -> dict[str, Any]:
        """The connection as last received; empty once it is gone."""
        data = self.coordinator.data
        return (data.connections.get(self._connection_id) if data else None) or {}

    @property
    def available(self) -> bool:
        """Unavailable once the connection is gone from the server."""
        return super().available and bool(self._connection)


def connection_ids(coordinator: OpenParcelsCoordinator) -> Iterable[str]:
    """The ids of the connections currently known."""
    return coordinator.data.connections if coordinator.data else ()


def device_for_connection(entry: ConfigEntry, connection_id: str | None) -> DeviceInfo:
    """DeviceInfo pointing at a connection's device, or the hub's for None.

    Only the identifiers are needed: the coordinator registers the connection
    devices with their full details.
    """
    if connection_id is None:
        return hub_device_info(entry)
    return DeviceInfo(
        identifiers={connection_identifier(entry.entry_id, connection_id)}
    )


def async_add_dynamic(
    coordinator: OpenParcelsCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    current_keys: Callable[[], Iterable[str]],
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for keys as they appear; the coordinator removes stale ones."""
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        keys = set(current_keys())
        new = keys - known
        known.intersection_update(keys)
        known.update(new)
        entities = [entity for key in sorted(new) for entity in factory(key)]
        if entities:
            async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
