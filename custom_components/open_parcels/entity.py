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
from .device import hub_device_info, source_identifier


class OpenParcelsEntity(CoordinatorEntity[OpenParcelsCoordinator]):
    """Base for every entity of this integration."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: OpenParcelsCoordinator, unique_suffix: str) -> None:
        """Set the unique id from the entry id and a suffix."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{unique_suffix}"


class OpenParcelsSourceEntity(OpenParcelsEntity):
    """Base for the entities of one source, on that source's device."""

    def __init__(
        self, coordinator: OpenParcelsCoordinator, source_id: str, key: str
    ) -> None:
        """Key the unique id on the source and place the entity on its device."""
        super().__init__(coordinator, f"source_{source_id}_{key}")
        self._source_id = source_id
        self._attr_device_info = device_for_source(coordinator.config_entry, source_id)

    @property
    def _source(self) -> dict[str, Any]:
        """The source as last received; empty once it is gone."""
        data = self.coordinator.data
        return (data.sources.get(self._source_id) if data else None) or {}

    @property
    def available(self) -> bool:
        """Unavailable once the source is gone from the server."""
        return super().available and bool(self._source)


def source_ids(coordinator: OpenParcelsCoordinator) -> Iterable[str]:
    """The ids of the sources currently known."""
    return coordinator.data.sources if coordinator.data else ()


def device_for_source(entry: ConfigEntry, source_id: str | None) -> DeviceInfo:
    """DeviceInfo pointing at a source's device, or the hub's for None.

    Only the identifiers are needed: the coordinator registers the source
    devices with their full details.
    """
    if source_id is None:
        return hub_device_info(entry)
    return DeviceInfo(identifiers={source_identifier(entry.entry_id, source_id)})


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
