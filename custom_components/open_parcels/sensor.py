"""Sensors: hub summaries, per-connection summaries and diagnostics.

Parcels themselves are device trackers (device_tracker.py); delivered ones only
appear here, in the list sensors' attributes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
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
from .parcels import count_by_carrier, next_delivery, parse_iso
from .returns import return_summary

# The coordinator fans updates out to every entity.
PARALLEL_UPDATES = 0

# Hub list sensors use the aggregator's keys.
HUB_LIST_KEYS = (
    "receiving",
    "sending",
    "delivered",
    "sending_delivered",
    "en_route_to_pickup_point",
    "awaiting_pickup",
)

# Connection list sensors use the per-carrier keys -> the bucket they show.
CONNECTION_LIST_KEYS = {
    "receiving_parcels": "receiving",
    "en_route_to_pickup_point": "en_route_to_pickup_point",
    "awaiting_pickup": "awaiting_pickup",
    "delivered_parcels": "delivered",
    "sending_parcels": "sending",
    "sending_delivered_parcels": "sending_delivered",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenParcelsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors and keep adding them as connections and parcels appear."""
    coordinator = entry.runtime_data.coordinator

    hub: list[SensorEntity] = [
        ListSensor(coordinator, key, None, key) for key in HUB_LIST_KEYS
    ]
    hub.append(NextDeliverySensor(coordinator, None, "next_delivery"))
    hub.append(OpenReturnsSensor(coordinator))
    hub.append(LastUpdateSensor(coordinator))
    async_add_entities(hub)

    def _connection_entities(connection_id: str) -> list[Entity]:
        entities: list[Entity] = [
            ListSensor(coordinator, bucket, connection_id, key)
            for key, bucket in CONNECTION_LIST_KEYS.items()
        ]
        entities.append(NextDeliverySensor(coordinator, connection_id, "next_delivery"))
        entities.append(ConnectionLastOkSensor(coordinator, connection_id))
        entities.append(ConnectionStateSensor(coordinator, connection_id))
        return entities

    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        lambda: connection_ids(coordinator),
        _connection_entities,
    )
    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        coordinator.order_keys,
        lambda key: [OrderSensor(coordinator, key)],
    )


def _suffix(connection_id: str | None, key: str) -> str:
    return key if connection_id is None else f"connection_{connection_id}_{key}"


class ListSensor(OpenParcelsEntity, SensorEntity):
    """Count of one parcel list, with the list and a per-carrier breakdown."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"parcels", "by_carrier"})

    def __init__(
        self,
        coordinator: OpenParcelsCoordinator,
        bucket: str,
        connection_id: str | None,
        key: str,
    ) -> None:
        """Show ``bucket`` for the hub (connection_id None) or one connection."""
        super().__init__(coordinator, _suffix(connection_id, key))
        self._bucket = bucket
        self._connection_id = connection_id
        self._attr_translation_key = key
        self._attr_device_info = device_for_connection(
            coordinator.config_entry, connection_id
        )

    def _parcels(self) -> list[dict[str, Any]]:
        return self.coordinator.buckets(self._connection_id)[self._bucket]

    @property
    def native_value(self) -> int:
        """Number of parcels in the list."""
        return len(self._parcels())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The list itself, its parcel ids and its breakdown per carrier."""
        parcels = self._parcels()
        return {
            "by_carrier": count_by_carrier(parcels),
            "parcel_ids": [p["id"] for p in parcels if p.get("id")],
            "parcels": parcels,
        }


class NextDeliverySensor(OpenParcelsEntity, SensorEntity):
    """Earliest expected delivery among the active receiving parcels."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "next_delivery"
    _unrecorded_attributes = frozenset(
        {"parcel", "by_carrier", "tracking_number", "sender", "receiver"}
    )

    def __init__(
        self, coordinator: OpenParcelsCoordinator, connection_id: str | None, key: str
    ) -> None:
        """Show the hub's (connection_id None) or one connection's next delivery."""
        super().__init__(coordinator, _suffix(connection_id, key))
        self._connection_id = connection_id
        self._attr_device_info = device_for_connection(
            coordinator.config_entry, connection_id
        )

    def _compute(
        self,
    ) -> tuple[datetime | None, dict[str, Any] | None, dict[str, datetime]]:
        return next_delivery(self.coordinator.buckets(self._connection_id)["receiving"])

    @property
    def native_value(self) -> datetime | None:
        """The earliest expected_from."""
        return self._compute()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The aggregator's attributes plus the carriers' tracking_number/sender/receiver."""
        _, parcel, by_carrier = self._compute()
        return {
            "tracking_number": parcel.get("tracking_number") if parcel else None,
            "sender": parcel.get("sender") if parcel else None,
            "receiver": parcel.get("receiver") if parcel else None,
            "by_carrier": {label: dt.isoformat() for label, dt in by_carrier.items()},
            "parcel": parcel,
        }


class OpenReturnsSensor(OpenParcelsEntity, SensorEntity):
    """Returns still going on, soonest return-by day first."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "open_returns"
    _unrecorded_attributes = frozenset({"returns"})

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "open_returns")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

    @property
    def native_value(self) -> int:
        """Number of open returns."""
        return len(self.coordinator.open_returns())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Each open return in short: order, shop, status, deadline, drop-off, cost."""
        return {
            "returns": [return_summary(ret) for ret in self.coordinator.open_returns()]
        }


class LastUpdateSensor(OpenParcelsEntity, SensorEntity):
    """When the last full resync from the server succeeded."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_update"

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "last_update")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

    @property
    def native_value(self) -> datetime | None:
        """Time of the last successful resync."""
        return self.coordinator.last_success_time


class ConnectionLastOkSensor(OpenParcelsConnectionEntity, SensorEntity):
    """When the connection last ran without an error."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_update"

    def __init__(self, coordinator: OpenParcelsCoordinator, connection_id: str) -> None:
        """Belongs to the connection's device."""
        super().__init__(coordinator, connection_id, "last_update")

    @property
    def native_value(self) -> datetime | None:
        """The connection's last_ok_at."""
        return parse_iso(self._connection.get("last_ok_at"))


class ConnectionStateSensor(OpenParcelsConnectionEntity, SensorEntity):
    """The connection's state: ok, needs sign-in, error or disabled."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connection_state"
    _attr_options = [state.value for state in ConnectionState]

    def __init__(self, coordinator: OpenParcelsCoordinator, connection_id: str) -> None:
        """Belongs to the connection's device."""
        super().__init__(coordinator, connection_id, "connection_state")

    @property
    def native_value(self) -> str | None:
        """The state, or None for a value this version does not know.

        "idle" is a connection that ran but had nothing it could do (DPD with
        public tracking off); the detail says why.
        """
        state = self._connection.get("state")
        return state if state in self._attr_options else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Detail and run times as the server reports them."""
        connection = self._connection
        return {
            "detail": connection.get("state_detail"),
            "provider": connection.get("provider"),
            "kind": connection.get("kind"),
            "last_run_at": connection.get("last_run_at"),
            "next_run_at": connection.get("next_run_at"),
            "last_ok_at": connection.get("last_ok_at"),
        }


class OrderSensor(OpenParcelsEntity, SensorEntity):
    """One open order: how far it is, with its items and parcels (an option)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "order"
    _attr_options = ["in_progress", "partially_delivered"]
    _unrecorded_attributes = frozenset({"items", "parcels", "shop", "order_number"})

    def __init__(self, coordinator: OpenParcelsCoordinator, key: str) -> None:
        """Place the order on its shop's device (the hub when unknown)."""
        super().__init__(coordinator, f"order_{key}")
        self._key = key
        order = coordinator.open_orders().get(key) or {}
        self._attr_translation_placeholders = {
            "shop": str(order.get("shop") or ""),
            "number": str(order.get("order_number") or key),
        }
        self._attr_device_info = device_for_connection(
            coordinator.config_entry, order.get("connection_id")
        )

    @property
    def _order(self) -> dict[str, Any] | None:
        return self.coordinator.open_orders().get(self._key)

    @property
    def available(self) -> bool:
        """Unavailable once the order is finished (the entity is then removed)."""
        return super().available and self._order is not None

    @property
    def native_value(self) -> str | None:
        """In progress, or partially delivered once some of its parcels arrived."""
        order = self._order
        if order is None:
            return None
        delivered = [parcel for parcel in order["parcels"] if parcel["delivered"]]
        return "partially_delivered" if delivered else "in_progress"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Shop, number, counts, items (with their parcel's code) and parcels."""
        order = self._order or {}
        parcels = order.get("parcels") or []
        codes = {parcel["id"]: parcel["tracking_number"] for parcel in parcels}
        return {
            "shop": order.get("shop"),
            "order_number": order.get("order_number"),
            "parcel_count": len(parcels),
            "delivered_count": sum(1 for parcel in parcels if parcel["delivered"]),
            "items": [
                {**item, "parcel": codes.get(item.get("parcel_id"))}
                for item in order.get("items") or []
                if isinstance(item, dict)
            ],
            "parcels": parcels,
        }
