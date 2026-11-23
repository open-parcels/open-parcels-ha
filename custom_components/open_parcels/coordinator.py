"""Data coordinator: a slow REST resync plus live updates from the event stream."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OpenParcelsAuthError, OpenParcelsClient, OpenParcelsError, SseMessage
from .const import (
    CALENDAR_FUTURE_DAYS,
    CALENDAR_PAST_DAYS,
    CONF_INCLUDE_HISTORY,
    CONF_ORDER_ENTITIES,
    CONF_RESYNC_MINUTES,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_ORDER_ENTITIES,
    DEFAULT_RESYNC_MINUTES,
    DOMAIN,
    LIVE_POSITION_EVENT,
    LIVE_TRACKING_STATES,
    OPEN_STATUSES,
    PARCEL_LIST_LIMIT,
    READY_EVENT,
    RESYNC_EVENT,
    SERVER_EVENT_SUFFIXES,
    SETTINGS_UPDATED_EVENT,
    SIGNAL_LIVE_POSITION,
    SOURCE_REMOVED_EVENT,
    SOURCE_SNAPSHOT_EVENTS,
)
from .device import find_device, hub_identifier, source_device_info, source_identifier
from .parcels import (
    apply_delivered_filter,
    effective_status,
    is_delivered,
    order_key,
    owner_source_id,
    sort_parcels_by_ts,
    to_suite_parcel,
)

_LOGGER = logging.getLogger(__name__)

# Events after which the calendar's cached entries may be out of date.
_CALENDAR_EVENTS = frozenset(
    {
        "parcel.registered",
        "parcel.delivered",
        "parcel.delivery_time_changed",
        "parcel.outgoing_registered",
        "parcel.outgoing_delivered",
        "parcel.removed",
        "parcel.deleted",
        "parcel.status_changed",
    }
)

# Parcel events after which the parcel is gone from the server's lists.
_GONE_EVENTS = frozenset({"parcel.removed", "parcel.deleted"})


@dataclass(slots=True)
class CourierPosition:
    """The courier's current position; kept in memory only."""

    latitude: float
    longitude: float
    accuracy: float | None
    position_at: str | None
    stops_away: int | None


@dataclass(frozen=True, slots=True)
class ParcelPosition:
    """Where a parcel's tracker points: the live courier, or its latest scan."""

    latitude: float
    longitude: float
    accuracy: float
    at: str | None
    source: str
    stops_away: int | None


@dataclass(slots=True)
class OpenParcelsData:
    """Everything the entities read, as last received from the server."""

    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    carriers: dict[str, str] = field(default_factory=dict)
    parcels: dict[str, dict[str, Any]] = field(default_factory=dict)


def _position_from_live(live: Any) -> CourierPosition | None:
    if not isinstance(live, Mapping) or live.get("state") not in LIVE_TRACKING_STATES:
        return None
    lat, lng = live.get("latitude"), live.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    accuracy = live.get("accuracy")
    stops = live.get("stops_away")
    return CourierPosition(
        latitude=float(lat),
        longitude=float(lng),
        accuracy=float(accuracy) if isinstance(accuracy, (int, float)) else None,
        position_at=live.get("position_at"),
        stops_away=stops if isinstance(stops, int) else None,
    )


def _older(parcel: Mapping[str, Any], stored: Mapping[str, Any] | None) -> bool:
    """Whether an event's parcel is older than the stored one, by `updated_at`."""
    if stored is None:
        return False
    new = dt_util.parse_datetime(str(parcel.get("updated_at") or ""))
    old = dt_util.parse_datetime(str(stored.get("updated_at") or ""))
    return new is not None and old is not None and new < old


class OpenParcelsCoordinator(DataUpdateCoordinator[OpenParcelsData]):
    """Holds sources, carriers and parcels; the event stream keeps them current."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: OpenParcelsClient
    ) -> None:
        """Initialise with the slow resync interval from the options."""
        minutes = int(entry.options.get(CONF_RESYNC_MINUTES, DEFAULT_RESYNC_MINUTES))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.positions: dict[str, CourierPosition] = {}
        self.calendar_entries: list[dict[str, Any]] = []
        self.last_success_time: datetime | None = None
        self.stream_connected = False
        self.last_event_id: str | None = None
        self.events_received = 0
        self._refresh_wanted = False
        self._calendar_debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=10,
            immediate=False,
            function=self.async_refresh_calendar,
        )

    # ------------------------------------------------------------------ views

    @property
    def include_history(self) -> bool:
        """Whether parcels carry their status history."""
        return bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def suite_parcel(self, parcel_id: str) -> dict[str, Any] | None:
        """The suite-shaped parcel for an id, or None when it is gone."""
        raw = (self.data.parcels if self.data else {}).get(parcel_id)
        if raw is None:
            return None
        return to_suite_parcel(
            raw, include_history=self.include_history, carrier_names=self.data.carriers
        )

    def _suite_parcels(self) -> list[dict[str, Any]]:
        if not self.data:
            return []
        return [
            to_suite_parcel(
                raw,
                include_history=self.include_history,
                carrier_names=self.data.carriers,
            )
            for raw in self.data.parcels.values()
        ]

    def buckets(self, source_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Split parcels into the suite's lists, optionally for one source only.

        ``source_id`` of None means every parcel; pass a source id to get only
        the parcels whose device is that source.
        """
        parcels = self._suite_parcels()
        if source_id is not None:
            parcels = [p for p in parcels if p["source_id"] == source_id]
        options = self.config_entry.options
        incoming = [p for p in parcels if p["direction"] != "outgoing"]
        outgoing = [p for p in parcels if p["direction"] == "outgoing"]
        active_in = sort_parcels_by_ts(
            [p for p in incoming if not p["delivered"]], "planned_from"
        )
        active_out = sort_parcels_by_ts(
            [p for p in outgoing if not p["delivered"]], "planned_from"
        )
        delivered_in = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in incoming if p["delivered"]], "delivered_at", descending=True
            ),
            options,
        )
        delivered_out = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in outgoing if p["delivered"]], "delivered_at", descending=True
            ),
            options,
        )
        return {
            "incoming": active_in,
            "outgoing": active_out,
            "delivered": delivered_in,
            "outgoing_delivered": delivered_out,
            "en_route_to_pickup_point": [
                p for p in active_in if p["pickup"] and p["status"] != "at_pickup_point"
            ],
            "awaiting_pickup": [
                p for p in active_in if p["status"] == "at_pickup_point"
            ],
        }

    def tracked_parcel_ids(self) -> set[str]:
        """Parcels that get a tracker entity: the open ones (see OPEN_STATUSES).

        Delivered, finished and unknown parcels only appear in the list sensors.
        """
        buckets = self.buckets()
        return {
            str(p["id"])
            for key in ("incoming", "outgoing")
            for p in buckets[key]
            if p.get("id") and p.get("status") in OPEN_STATUSES
        }

    @property
    def order_entities(self) -> bool:
        """Whether open orders get a sensor each (an option, off by default)."""
        return bool(
            self.config_entry.options.get(CONF_ORDER_ENTITIES, DEFAULT_ORDER_ENTITIES)
        )

    def open_orders(self) -> dict[str, dict[str, Any]]:
        """Orders with a parcel still on its way, by a stable key.

        Each has its shop, number, items (with the carrying parcel's code when
        known) and parcels (id, code, name, status, delivered).
        """
        orders: dict[str, dict[str, Any]] = {}
        for raw in (self.data.parcels if self.data else {}).values():
            order = raw.get("order")
            if not isinstance(order, dict) or raw.get("direction") == "outgoing":
                continue
            shop, number = (
                str(order.get("shop") or ""),
                str(order.get("order_number") or ""),
            )
            key = order_key(shop, number)
            entry = orders.setdefault(
                key,
                {
                    "shop": shop,
                    "order_number": number,
                    "items": list(order.get("items") or []),
                    "parcels": [],
                    "source_id": owner_source_id(raw),
                },
            )
            entry["parcels"].append(
                {
                    "id": raw.get("id"),
                    "barcode": raw.get("barcode"),
                    "name": raw.get("display_name") or raw.get("barcode"),
                    "status": effective_status(raw).value,
                    "delivered": is_delivered(raw),
                }
            )
        return {
            key: order
            for key, order in orders.items()
            if not all(parcel["delivered"] for parcel in order["parcels"])
        }

    def order_keys(self) -> set[str]:
        """Orders that get a sensor: the open ones, when the option is on."""
        return set(self.open_orders()) if self.order_entities else set()

    def parcel_position(self, parcel_id: str) -> ParcelPosition | None:
        """Where a parcel is: the live courier first, else its latest scan with a place."""
        courier = self.positions.get(parcel_id)
        if courier is not None:
            return ParcelPosition(
                latitude=courier.latitude,
                longitude=courier.longitude,
                accuracy=courier.accuracy or 0,
                at=courier.position_at,
                source="courier",
                stops_away=courier.stops_away,
            )
        raw = (self.data.parcels if self.data else {}).get(parcel_id) or {}
        location = raw.get("location") or {}
        latitude, longitude = location.get("latitude"), location.get("longitude")
        if not isinstance(latitude, int | float) or not isinstance(
            longitude, int | float
        ):
            return None
        # The server's answer to "where is it now": the courier, else the newest placed scan.
        return ParcelPosition(
            latitude=float(latitude),
            longitude=float(longitude),
            accuracy=0,
            at=location.get("at"),
            source="courier" if location.get("source") == "courier" else "scan",
            stops_away=None,
        )

    def device_id_for_source(self, source_id: str | None) -> str | None:
        """Device registry id of a source's device, or the hub's for None."""
        entry_id = self.config_entry.entry_id
        identifier = (
            source_identifier(entry_id, source_id)
            if source_id
            else hub_identifier(entry_id)
        )
        device = find_device(self.hass, entry_id, identifier)
        return device.id if device else None

    # -------------------------------------------------------------- refreshes

    async def _async_update_data(self) -> OpenParcelsData:
        try:
            sources, carriers, parcels = await asyncio.gather(
                self.client.async_get_sources(),
                self.client.async_get_carriers(),
                self.client.async_get_parcels(
                    include_history=self.include_history, limit=PARCEL_LIST_LIMIT
                ),
            )
        except OpenParcelsAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except OpenParcelsError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        data = OpenParcelsData(
            sources={str(s["id"]): s for s in sources if s.get("id")},
            carriers={
                str(c["id"]): str(c.get("name") or c["id"])
                for c in carriers
                if c.get("id")
            },
            parcels={str(p["id"]): p for p in parcels if p.get("id")},
        )
        self.positions = {
            pid: pos
            for pid, raw in data.parcels.items()
            if (pos := _position_from_live(raw.get("live"))) is not None
        }
        self.last_success_time = datetime.now(UTC)
        await self.async_refresh_calendar()
        return data

    async def async_refresh_calendar(self) -> None:
        """Re-read the calendar entries the calendar entity shows as its state."""
        now = dt_util.utcnow()
        try:
            self.calendar_entries = await self.client.async_get_calendar(
                now - timedelta(days=CALENDAR_PAST_DAYS),
                now + timedelta(days=CALENDAR_FUTURE_DAYS),
            )
        except OpenParcelsError as err:
            _LOGGER.debug("Could not refresh the calendar: %s", err)
            return
        if self.data is not None:
            self.async_update_listeners()

    @callback
    def async_sync_registry(self) -> None:
        """Create a device per source and drop devices and entities that are gone."""
        if self.data is None:
            return
        entry = self.config_entry
        devices = dr.async_get(self.hass)
        hub = find_device(self.hass, entry.entry_id, hub_identifier(entry.entry_id))
        for source in self.data.sources.values():
            info = source_device_info(entry, source, self.data.carriers)
            device = devices.async_get_or_create(config_entry_id=entry.entry_id, **info)
            if hub is not None and device.via_device_id != hub.id:
                devices.async_update_device(device.id, via_device_id=hub.id)

        tracked = self.tracked_parcel_ids()
        orders = self.order_keys()
        entities = er.async_get(self.hass)
        parcel_prefix = f"{entry.entry_id}_parcel_"
        courier_prefix = f"{entry.entry_id}_courier_"
        order_prefix = f"{entry.entry_id}_order_"
        for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
            uid = entity.unique_id
            if uid.startswith(order_prefix):
                # Finished orders, and every order once the option is off.
                if uid[len(order_prefix) :] not in orders:
                    entities.async_remove(entity.entity_id)
                continue
            # Earlier versions had a status sensor per parcel and a separate courier
            # tracker; one tracker per open parcel replaces both.
            legacy = uid.startswith(courier_prefix) or (
                entity.domain == "sensor" and uid.startswith(parcel_prefix)
            )
            if legacy:
                entities.async_remove(entity.entity_id)
                continue
            if not uid.startswith(parcel_prefix):
                continue
            parcel_id = uid[len(parcel_prefix) :]
            if parcel_id not in tracked:
                entities.async_remove(entity.entity_id)
                continue
            # A parcel whose owning source was removed or changed moves to its new
            # owner's device first, so removing the old device does not take it along.
            owner = self.device_id_for_source(
                owner_source_id(self.data.parcels.get(parcel_id) or {})
            )
            if owner is not None and entity.device_id != owner:
                entities.async_update_entity(entity.entity_id, device_id=owner)

        wanted = {
            source_identifier(entry.entry_id, sid) for sid in self.data.sources
        } | {hub_identifier(entry.entry_id)}
        # A device belongs to exactly one config entry, so one this entry no longer
        # wants is removed outright (remove_config_entry_id breaks in HA 2027.8).
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
            if not device.identifiers & wanted:
                devices.async_remove_device(device.id)

    # ------------------------------------------------------------ the stream

    @callback
    def async_set_stream_connected(self, connected: bool) -> None:
        """Record the event stream's state for the diagnostic entity."""
        if self.stream_connected != connected:
            self.stream_connected = connected
            if self.data is not None:
                self.async_update_listeners()

    def _publish(self) -> None:
        """Tell the entities the data changed.

        Not async_set_updated_data: that cancels a refresh already queued on the
        debouncer and restarts the resync timer, so a steady flow of events would
        drop refresh requests and postpone the periodic resync forever.
        """
        self.async_update_listeners()

    def _want_refresh(self) -> None:
        """Ask for one REST reload once the current message is handled."""
        self._refresh_wanted = True

    @callback
    def async_start_refresh(self, *, debounced: bool) -> None:
        """Reload over REST in a task tied to the entry."""
        self.config_entry.async_create_task(
            self.hass,
            self.async_request_refresh() if debounced else self.async_refresh(),
            f"{DOMAIN}_refresh",
        )

    @callback
    def async_handle_message(self, message: SseMessage) -> None:
        """Apply one event-stream message, then reload once if it asked for that."""
        self._refresh_wanted = False
        self._handle_message(message)
        if self._refresh_wanted:
            self._refresh_wanted = False
            self.async_start_refresh(debounced=True)

    def _handle_message(self, message: SseMessage) -> None:
        """Apply one event-stream message and fire the matching bus event."""
        try:
            payload = json.loads(message.data) if message.data else {}
        except ValueError:
            _LOGGER.debug("Ignoring a malformed %s message", message.event)
            return
        if not isinstance(payload, dict):
            return
        if message.id is not None:
            # `resync` and `ready` carry the server's current sequence: continuing
            # from it is the reset the server asks for.
            self.last_event_id = message.id
        self.events_received += 1

        if message.event == RESYNC_EVENT:
            _LOGGER.info(
                "The server could not replay missed events (%s); reloading everything",
                payload.get("reason", "unknown"),
            )
            # Not debounced: the reload must not wait behind a cooldown.
            self.async_start_refresh(debounced=False)
            return
        if message.event == READY_EVENT:
            return
        if message.event == SETTINGS_UPDATED_EVENT:
            # How parcels are named (or the home location) changed on the server:
            # every parcel's display name may differ, so read everything again.
            self.async_start_refresh(debounced=False)
            return
        if message.event == LIVE_POSITION_EVENT:
            self._apply_position(payload)
            return
        if message.event == SOURCE_REMOVED_EVENT and self.data is not None:
            self._apply_source_removed(payload)
            self._calendar_debouncer.async_schedule_call()
            return
        if message.event in SOURCE_SNAPSHOT_EVENTS and self.data is not None:
            self._apply_source_snapshot(payload)
            return
        if message.event not in SERVER_EVENT_SUFFIXES or self.data is None:
            return
        if message.event == "source.state_changed":
            self._apply_source_event(payload)
        else:
            self._apply_parcel_event(message.event, payload)
        if message.event in _CALENDAR_EVENTS:
            self._calendar_debouncer.async_schedule_call()

    def _apply_position(self, payload: dict[str, Any]) -> None:
        parcel_id = payload.get("parcel_id")
        lat, lng = payload.get("latitude"), payload.get("longitude")
        if not isinstance(parcel_id, str):
            return
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            self.positions.pop(parcel_id, None)
        else:
            accuracy = payload.get("accuracy")
            stops = payload.get("stops_away")
            self.positions[parcel_id] = CourierPosition(
                latitude=float(lat),
                longitude=float(lng),
                accuracy=float(accuracy)
                if isinstance(accuracy, (int, float))
                else None,
                position_at=payload.get("position_at") or payload.get("positionAt"),
                stops_away=stops if isinstance(stops, int) else None,
            )
        async_dispatcher_send(
            self.hass,
            SIGNAL_LIVE_POSITION.format(self.config_entry.entry_id),
            parcel_id,
        )

    def _apply_source_event(self, payload: dict[str, Any]) -> None:
        raw_source = payload.get("source")
        source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
        source_id = str(source.get("id") or "")
        known = self.data.sources.get(source_id)
        if known is None:
            # A source this entry has not seen yet: pick it up with a resync.
            self._want_refresh()
        else:
            known["state"] = payload.get("new_state", known.get("state"))
            known["state_detail"] = payload.get("detail")
        self.hass.bus.async_fire(
            f"{DOMAIN}_source_state_changed",
            {
                "source_id": source_id,
                "plugin": source.get("plugin"),
                "label": source.get("label"),
                "old_state": payload.get("old_state"),
                "new_state": payload.get("new_state"),
                "detail": payload.get("detail"),
                "device_id": self.device_id_for_source(source_id or None),
                "hub_device_id": self.device_id_for_source(None),
            },
        )
        self._publish()

    def _apply_source_snapshot(self, payload: dict[str, Any]) -> None:
        """Take a source as an event reports it: added, renamed, or just run.

        The registry sync that follows creates a new source's device at once and
        renames an existing one; a name the user gave the device in Home Assistant
        is kept, since that is stored apart. A source not seen before is also
        reloaded over REST for the fields the event does not carry (run times).
        """
        raw_source = payload.get("source")
        source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
        source_id = str(source.get("id") or "")
        if not source_id:
            return
        known = self.data.sources.get(source_id)
        if known is None:
            known = self.data.sources[source_id] = {"id": source_id}
            self._want_refresh()
        for key in ("plugin", "label", "kind"):
            if key in source:
                known[key] = source[key]
        if "state" in payload:
            known["state"] = payload["state"]
            known["state_detail"] = payload.get("detail")
        self._publish()

    def _apply_source_removed(self, payload: dict[str, Any]) -> None:
        """Drop a source the server deleted or merged, with its device.

        Its parcels lose the link; the registry sync then removes the device and
        reloads the entry, so parcels it owned come back on their new owner.
        """
        raw_source = payload.get("source")
        source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
        source_id = str(source.get("id") or "")
        if not source_id:
            return
        # Read the device before the registry sync removes it.
        device_id = self.device_id_for_source(source_id)
        self.data.sources.pop(source_id, None)
        merged_into = payload.get("merged_into")
        for pid, parcel in list(self.data.parcels.items()):
            refs = parcel.get("sources") or []
            kept = [
                ref
                for ref in refs
                if not (isinstance(ref, Mapping) and ref.get("source_id") == source_id)
            ]
            if len(kept) != len(refs):
                self.data.parcels[pid] = {**parcel, "sources": kept}
        if isinstance(merged_into, str) and merged_into not in self.data.sources:
            self._want_refresh()
        self.hass.bus.async_fire(
            f"{DOMAIN}_source_removed",
            {
                "source_id": source_id,
                "plugin": source.get("plugin"),
                "label": source.get("label"),
                "merged_into": merged_into,
                "device_id": device_id,
                "hub_device_id": self.device_id_for_source(None),
            },
        )
        self._publish()

    def _apply_parcel_event(self, event_type: str, payload: dict[str, Any]) -> None:
        parcel = payload.get("parcel")
        if not isinstance(parcel, dict) or not parcel.get("id"):
            return
        parcel_id = str(parcel["id"])
        previous = self.data.parcels.get(parcel_id)
        if event_type not in _GONE_EVENTS and _older(parcel, previous):
            # A replayed event older than what the last REST load returned: the
            # bus event still fires (it happened), the newer stored state stays.
            pass
        elif event_type in _GONE_EVENTS:
            self.data.parcels.pop(parcel_id, None)
            self.positions.pop(parcel_id, None)
        else:
            if (
                not self.include_history
                and previous is not None
                and "history" not in parcel
            ):
                parcel = {**parcel, "history": previous.get("history", [])}
            self.data.parcels[parcel_id] = parcel
            live = parcel.get("live")
            if (
                not isinstance(live, dict)
                or live.get("state") not in LIVE_TRACKING_STATES
            ):
                self.positions.pop(parcel_id, None)

        source_id = owner_source_id(parcel)
        if source_id is None and event_type in _GONE_EVENTS and previous is not None:
            # A removal's payload may no longer name the parcel's sources: the trigger still
            # belongs on the device of the source that had it, not on the hub.
            source_id = owner_source_id(previous)
        if source_id and source_id not in self.data.sources:
            self._want_refresh()

        suite = to_suite_parcel(
            parcel,
            include_history=self.include_history,
            carrier_names=self.data.carriers,
        )
        suite.pop("raw", None)
        changes = {k: v for k, v in payload.items() if k != "parcel"}
        self.hass.bus.async_fire(
            f"{DOMAIN}_{SERVER_EVENT_SUFFIXES[event_type]}",
            {
                **suite,
                **changes,
                "device_id": self.device_id_for_source(source_id),
                "hub_device_id": self.device_id_for_source(None),
            },
        )
        self._publish()

    async def async_shutdown(self) -> None:
        """Cancel the calendar debouncer too."""
        await super().async_shutdown()
        self._calendar_debouncer.async_shutdown()
