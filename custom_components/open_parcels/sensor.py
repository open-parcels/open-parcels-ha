"""Sensors: hub summaries, per-source summaries and diagnostics.

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
from .const import NOT_RUN_YET_DETAIL, SourceState
from .coordinator import OpenParcelsCoordinator
from .entity import OpenParcelsEntity, async_add_dynamic, device_for_source
from .parcels import count_by_carrier, next_delivery, parse_iso
from .returns import return_summary

# The coordinator fans updates out to every entity.
PARALLEL_UPDATES = 0

# Hub list sensors use the aggregator's keys.
HUB_LIST_KEYS = (
    "incoming",
    "outgoing",
    "delivered",
    "outgoing_delivered",
    "en_route_to_pickup_point",
    "awaiting_pickup",
)

# Source list sensors use the per-carrier keys -> the bucket they show.
SOURCE_LIST_KEYS = {
    "incoming_parcels": "incoming",
    "en_route_to_pickup_point": "en_route_to_pickup_point",
    "awaiting_pickup": "awaiting_pickup",
    "delivered_parcels": "delivered",
    "outgoing_parcels": "outgoing",
    "outgoing_delivered_parcels": "outgoing_delivered",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenParcelsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors and keep adding them as sources and parcels appear."""
    coordinator = entry.runtime_data.coordinator

    hub: list[SensorEntity] = [
        ListSensor(coordinator, key, None, key) for key in HUB_LIST_KEYS
    ]
    hub.append(NextDeliverySensor(coordinator, None, "next_delivery"))
    hub.append(OpenReturnsSensor(coordinator))
    hub.append(LastUpdateSensor(coordinator))
    async_add_entities(hub)

    def _source_entities(source_id: str) -> list[Entity]:
        entities: list[Entity] = [
            ListSensor(coordinator, bucket, source_id, key)
            for key, bucket in SOURCE_LIST_KEYS.items()
        ]
        entities.append(NextDeliverySensor(coordinator, source_id, "next_delivery"))
        entities.append(SourceLastOkSensor(coordinator, source_id))
        entities.append(SourceStateSensor(coordinator, source_id))
        return entities

    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        lambda: coordinator.data.sources if coordinator.data else (),
        _source_entities,
    )
    async_add_dynamic(
        coordinator,
        entry,
        async_add_entities,
        coordinator.order_keys,
        lambda key: [OrderSensor(coordinator, key)],
    )


def _suffix(source_id: str | None, key: str) -> str:
    return key if source_id is None else f"source_{source_id}_{key}"


class ListSensor(OpenParcelsEntity, SensorEntity):
    """Count of one parcel list, with the list and a per-carrier breakdown."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"parcels", "by_carrier"})

    def __init__(
        self,
        coordinator: OpenParcelsCoordinator,
        bucket: str,
        source_id: str | None,
        key: str,
    ) -> None:
        """Show ``bucket`` for the hub (source_id None) or one source."""
        super().__init__(coordinator, _suffix(source_id, key))
        self._bucket = bucket
        self._source_id = source_id
        self._attr_translation_key = key
        self._attr_device_info = device_for_source(coordinator.config_entry, source_id)

    def _parcels(self) -> list[dict[str, Any]]:
        return self.coordinator.buckets(self._source_id)[self._bucket]

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
    """Earliest expected delivery among the active incoming parcels."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "next_delivery"
    _unrecorded_attributes = frozenset({"parcel", "by_carrier"})

    def __init__(
        self, coordinator: OpenParcelsCoordinator, source_id: str | None, key: str
    ) -> None:
        """Show the hub's (source_id None) or one source's next delivery."""
        super().__init__(coordinator, _suffix(source_id, key))
        self._source_id = source_id
        self._attr_device_info = device_for_source(coordinator.config_entry, source_id)

    def _compute(
        self,
    ) -> tuple[datetime | None, dict[str, Any] | None, dict[str, datetime]]:
        return next_delivery(self.coordinator.buckets(self._source_id)["incoming"])

    @property
    def native_value(self) -> datetime | None:
        """The earliest planned_from."""
        return self._compute()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The aggregator's attributes plus the carriers' barcode/sender/receiver."""
        _, parcel, by_carrier = self._compute()
        return {
            "barcode": parcel.get("barcode") if parcel else None,
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
        self._attr_device_info = device_for_source(coordinator.config_entry, None)

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
        self._attr_device_info = device_for_source(coordinator.config_entry, None)

    @property
    def native_value(self) -> datetime | None:
        """Time of the last successful resync."""
        return self.coordinator.last_success_time


class _SourceEntity(OpenParcelsEntity):
    def __init__(
        self, coordinator: OpenParcelsCoordinator, source_id: str, key: str
    ) -> None:
        super().__init__(coordinator, _suffix(source_id, key))
        self._source_id = source_id
        self._attr_device_info = device_for_source(coordinator.config_entry, source_id)

    @property
    def _source(self) -> dict[str, Any]:
        data = self.coordinator.data
        return (data.sources.get(self._source_id) if data else None) or {}

    @property
    def available(self) -> bool:
        """Unavailable once the source is gone from the server."""
        return super().available and bool(self._source)


class SourceLastOkSensor(_SourceEntity, SensorEntity):
    """When the source last ran without an error."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_update"

    def __init__(self, coordinator: OpenParcelsCoordinator, source_id: str) -> None:
        """Belongs to the source's device."""
        super().__init__(coordinator, source_id, "last_update")

    @property
    def native_value(self) -> datetime | None:
        """The source's last_ok_at."""
        return parse_iso(self._source.get("last_ok_at"))


class SourceStateSensor(_SourceEntity, SensorEntity):
    """The source's state: ok, needs sign-in, error or disabled."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "source_state"
    _attr_options = [state.value for state in SourceState]

    def __init__(self, coordinator: OpenParcelsCoordinator, source_id: str) -> None:
        """Belongs to the source's device."""
        super().__init__(coordinator, source_id, "source_state")

    @property
    def native_value(self) -> str | None:
        """The state, or None for a value this version does not know.

        The server reports "idle" for a source that ran but had nothing it could
        do (DPD with public tracking off). Older servers report that as "ok" with
        a detail other than "not run yet"; that still reads as "idle".
        """
        state = self._source.get("state")
        detail = self._source.get("state_detail")
        legacy_idle = (
            state == SourceState.OK and detail and detail != NOT_RUN_YET_DETAIL
        )
        if legacy_idle:
            return SourceState.IDLE.value
        return state if state in self._attr_options else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Detail and run times as the server reports them."""
        source = self._source
        return {
            "detail": source.get("state_detail"),
            "plugin": source.get("plugin"),
            "kind": source.get("kind"),
            "last_run_at": source.get("last_run_at"),
            "next_run_at": source.get("next_run_at"),
            "last_ok_at": source.get("last_ok_at"),
        }


class OrderSensor(OpenParcelsEntity, SensorEntity):
    """One open order: how far it is, with its items and parcels (an option)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "order"
    _attr_options = ["in_progress", "partially_delivered"]
    _unrecorded_attributes = frozenset({"items", "parcels"})

    def __init__(self, coordinator: OpenParcelsCoordinator, key: str) -> None:
        """Place the order on its shop's device (the hub when unknown)."""
        super().__init__(coordinator, f"order_{key}")
        self._key = key
        order = coordinator.open_orders().get(key) or {}
        self._attr_translation_placeholders = {
            "shop": str(order.get("shop") or ""),
            "number": str(order.get("order_number") or key),
        }
        self._attr_device_info = device_for_source(
            coordinator.config_entry, order.get("source_id")
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
        codes = {parcel["id"]: parcel["barcode"] for parcel in parcels}
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
