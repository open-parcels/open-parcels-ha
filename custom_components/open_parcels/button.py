"""Buttons: resync everything and back up on the hub, run one connection on its device."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenParcelsConfigEntry
from .api import OpenParcelsApiError, OpenParcelsError
from .const import DOMAIN
from .coordinator import OpenParcelsCoordinator
from .entity import (
    OpenParcelsConnectionEntity,
    OpenParcelsEntity,
    async_add_dynamic,
    connection_ids,
    device_for_connection,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenParcelsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the hub's refresh button and one sync button per connection."""
    coordinator = entry.runtime_data.coordinator
    hub: list[ButtonEntity] = [RefreshButton(coordinator)]
    if coordinator.can_back_up:
        hub.append(BackUpButton(coordinator))
    async_add_entities(hub)
    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        lambda: connection_ids(coordinator),
        lambda connection_id: [ConnectionSyncButton(coordinator, connection_id)],
    )


class RefreshButton(OpenParcelsEntity, ButtonEntity):
    """Re-read everything from the server."""

    _attr_translation_key = "refresh"

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "refresh")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

    async def async_press(self) -> None:
        """Resync now."""
        await self.coordinator.async_request_refresh()


class ConnectionSyncButton(OpenParcelsConnectionEntity, ButtonEntity):
    """Ask the server to run this connection now."""

    _attr_translation_key = "refresh"

    def __init__(self, coordinator: OpenParcelsCoordinator, connection_id: str) -> None:
        """Belongs to the connection's device."""
        super().__init__(coordinator, connection_id, "sync")

    async def async_press(self) -> None:
        """Run the connection; its results arrive through the event stream."""
        try:
            await self.coordinator.client.async_sync_connection(self._connection_id)
        except OpenParcelsError as err:
            # A captcha pause or the carrier's rate limit: the server says how long.
            if (
                isinstance(err, OpenParcelsApiError)
                and err.status in (409, 429)
                and err.message
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="sync_backing_off",
                    translation_placeholders={"message": str(err.message)},
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sync_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class BackUpButton(OpenParcelsEntity, ButtonEntity):
    """Back the server's database up now; only with a token that holds the admin scope."""

    _attr_translation_key = "back_up"

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "back_up")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

    async def async_press(self) -> None:
        """Back up; the result arrives as `backup.finished` on the event stream."""
        try:
            await self.coordinator.client.async_back_up()
        except OpenParcelsError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="backup_failed",
                translation_placeholders={"error": str(err)},
            ) from err
