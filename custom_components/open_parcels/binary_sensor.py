"""Binary sensors: the server's health, the event stream, and per connection a problem flag."""

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
from .const import ConnectionState
from .coordinator import OpenParcelsCoordinator
from .entity import (
    OpenParcelsConnectionEntity,
    OpenParcelsEntity,
    async_add_dynamic,
    connection_ids,
    device_for_connection,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenParcelsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the stream sensor and one problem sensor per connection."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [EventStreamSensor(coordinator), ServerHealthSensor(coordinator)]
    )
    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        lambda: connection_ids(coordinator),
        lambda connection_id: [ConnectionProblemSensor(coordinator, connection_id)],
    )


class EventStreamSensor(OpenParcelsEntity, BinarySensorEntity):
    """Whether the live event stream is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "event_stream"

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "event_stream")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

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


class ConnectionProblemSensor(OpenParcelsConnectionEntity, BinarySensorEntity):
    """On while the connection needs a sign-in or fails."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connection_problem"

    def __init__(self, coordinator: OpenParcelsCoordinator, connection_id: str) -> None:
        """Belongs to the connection's device."""
        super().__init__(coordinator, connection_id, "problem")

    @property
    def is_on(self) -> bool:
        """A connection that needs a sign-in or reports an error is a problem."""
        return self._connection.get("state") in (
            ConnectionState.NEEDS_SIGN_IN,
            ConnectionState.ERROR,
        )


class ServerHealthSensor(OpenParcelsEntity, BinarySensorEntity):
    """On while the server is degraded: a part of it needs attention (GET /status)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "server_health"

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "server_health")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

    @property
    def _status(self) -> dict[str, Any]:
        data = self.coordinator.loaded_data
        return data.status if data is not None else {}

    @property
    def is_on(self) -> bool | None:
        """Anything but `ok` is a problem; unknown before the first status."""
        state = self._status.get("status")
        return None if state is None else state != "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The overall state, one state per part, and the server's version."""
        status = self._status
        return {
            "status": status.get("status"),
            "checks": status.get("checks"),
            "version": status.get("version"),
            "started_at": status.get("started_at"),
            "checked_at": status.get("checked_at"),
        }
