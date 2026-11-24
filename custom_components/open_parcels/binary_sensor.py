"""Binary sensors: the event stream's connection, and per source a problem flag."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenParcelsConfigEntry
from .const import SourceState
from .coordinator import OpenParcelsCoordinator
from .entity import (
    OpenParcelsEntity,
    OpenParcelsSourceEntity,
    async_add_dynamic,
    device_for_source,
    source_ids,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenParcelsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the stream sensor and one problem sensor per source."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([EventStreamSensor(coordinator)])
    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        lambda: source_ids(coordinator),
        lambda source_id: [SourceProblemSensor(coordinator, source_id)],
    )


class EventStreamSensor(OpenParcelsEntity, BinarySensorEntity):
    """Whether the live event stream is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "event_stream"

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "event_stream")
        self._attr_device_info = device_for_source(coordinator.config_entry, None)

    @property
    def available(self) -> bool:
        """Always available: a broken stream is what this entity reports."""
        return True

    @property
    def is_on(self) -> bool:
        """Connected or not."""
        return self.coordinator.stream_connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """How many events arrived since Home Assistant started."""
        return {"events_received": self.coordinator.events_received}


class SourceProblemSensor(OpenParcelsSourceEntity, BinarySensorEntity):
    """On while the source needs a sign-in or fails."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "source_problem"

    def __init__(self, coordinator: OpenParcelsCoordinator, source_id: str) -> None:
        """Belongs to the source's device."""
        super().__init__(coordinator, source_id, "problem")

    @property
    def is_on(self) -> bool:
        """A source that needs a sign-in or reports an error is a problem."""
        return self._source.get("state") in (
            SourceState.NEEDS_SIGN_IN,
            SourceState.ERROR,
        )
