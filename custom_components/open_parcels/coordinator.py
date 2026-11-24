"""Data coordinator: a slow REST resync plus live updates from the event stream."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    OPEN_STATUSES,
    ORDER_DELETED_EVENT,
    ORDER_UPDATED_EVENT,
    PARCEL_LIST_LIMIT,
    PARCEL_LOCATION_EVENT,
    READY_EVENT,
    RECONNECT_REFRESH_DELAY_SECONDS,
    RESYNC_EVENT,
    RETURN_EVENT_SUFFIXES,
    SERVER_EVENT_SUFFIXES,
    SETTINGS_UPDATED_EVENT,
    SIGNAL_LIVE_POSITION,
    SOURCE_REMOVED_EVENT,
    SOURCE_SNAPSHOT_EVENTS,
    SOURCE_STATE_EVENT,
)
from .device import find_device, hub_identifier, source_device_info, source_identifier
from .merge import (
    GONE_EVENTS,
    CourierPosition,
    OpenParcelsData,
    drop_hidden,
    hidden,
    merge_order_deleted,
    merge_order_updated,
    merge_parcel,
    merge_position,
    merge_return,
    merge_source_removed,
    merge_source_snapshot,
    merge_source_state,
    position_from_live,
    reapply,
    return_of,
    source_of,
)
from .parcels import (
    apply_delivered_filter,
    effective_status,
    is_delivered,
    order_key,
    owner_source_id,
    sort_parcels_by_ts,
    to_suite_parcel,
)
from .returns import return_summary

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
        *RETURN_EVENT_SUFFIXES,
    }
)


# Parcel keys left out of bus events (see _fire_parcel_event).
_EVENT_DROPPED_KEYS = ("raw", "items", "order", "history", "live", "lookup")


@dataclass(frozen=True, slots=True)
class ParcelPosition:
    """Where a parcel's tracker points: the live courier, or its latest scan."""

    latitude: float
    longitude: float
    accuracy: float
    at: str | None
    source: str
    stops_away: int | None


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
        # Bus-event deduplication: the newest server sequence handled, whether the
        # current message repeats one, and the change last fired per event and id.
        self._max_seq: int | None = None
        self._repeat = False
        self._fired: dict[tuple[str, str], str] = {}
        # One reload at a time; while one runs, the messages it may predate.
        self._reload_lock = asyncio.Lock()
        self._reload_log: list[tuple[str, dict[str, Any]]] | None = None
        self._reconnect_debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=RECONNECT_REFRESH_DELAY_SECONDS,
            immediate=False,
            function=self.async_request_refresh,
        )
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
        # One reload at a time: of two overlapping ones, the older could finish last.
        async with self._reload_lock:
            self._reload_log = []
            try:
                return await self._async_load()
            finally:
                self._reload_log = None

    async def _async_load(self) -> OpenParcelsData:
        try:
            sources, carriers, parcels, returns = await asyncio.gather(
                self.client.async_get_sources(),
                self.client.async_get_carriers(),
                self.client.async_get_parcels(
                    include_history=self.include_history, limit=PARCEL_LIST_LIMIT
                ),
                self.client.async_get_returns(),
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
        await self.async_refresh_calendar()

        # From here on nothing awaits, so no message slips in between the replay
        # below and the coordinator taking this snapshot as its data.
        data = OpenParcelsData(
            sources={str(s["id"]): s for s in sources if s.get("id")},
            carriers={
                str(c["id"]): str(c.get("name") or c["id"])
                for c in carriers
                if c.get("id")
            },
            parcels={str(p["id"]): p for p in parcels if p.get("id")},
            returns={str(r["id"]): r for r in returns if r.get("id")},
        )
        self.positions = {
            pid: pos
            for pid, raw in data.parcels.items()
            if (pos := position_from_live(raw.get("live"))) is not None
        }
        # Messages that arrived while the requests ran may be newer than the
        # snapshot: apply them again (stale ones are skipped by `updated_at`).
        for event_type, payload in self._reload_log or []:
            reapply(
                data,
                self.positions,
                event_type,
                payload,
                include_history=self.include_history,
            )
        self.last_success_time = datetime.now(UTC)
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
        if message.truncated:
            # Too big to keep: what it changed comes with a reload instead.
            if message.id is not None:
                self.last_event_id = message.id
            self._note_sequence(message)
            _LOGGER.warning(
                "The %s event was too large to read; reloading everything",
                message.event,
            )
            self._want_refresh()
            return
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
        self._note_sequence(message)
        self.events_received += 1

        event = message.event
        if event == RESYNC_EVENT:
            _LOGGER.info(
                "The server could not replay missed events (%s); reloading everything",
                payload.get("reason", "unknown"),
            )
            self._reconnect_debouncer.async_cancel()
            # Not debounced: the reload must not wait behind a cooldown.
            self.async_start_refresh(debounced=False)
            return
        if event == READY_EVENT:
            return
        if event == SETTINGS_UPDATED_EVENT:
            # How parcels are named (or the home location) changed on the server:
            # every parcel's display name may differ, so read everything again.
            self.async_start_refresh(debounced=False)
            return
        if self._reload_log is not None:
            # A reload is in flight: its snapshot may predate this message.
            self._reload_log.append((event, payload))
        if event == LIVE_POSITION_EVENT:
            self._apply_position(payload)
            return
        if self.data is None:
            return
        if event == SOURCE_REMOVED_EVENT:
            self._apply_source_removed(payload)
            self._calendar_debouncer.async_schedule_call()
        elif event in SOURCE_SNAPSHOT_EVENTS:
            self._apply_source_snapshot(payload)
        elif event == SOURCE_STATE_EVENT:
            self._apply_source_event(payload)
        elif event in (ORDER_UPDATED_EVENT, ORDER_DELETED_EVENT):
            self._apply_order_event(event, payload)
        elif event in RETURN_EVENT_SUFFIXES:
            self._apply_return_event(event, payload)
            self._calendar_debouncer.async_schedule_call()
        elif event == PARCEL_LOCATION_EVENT or event in SERVER_EVENT_SUFFIXES:
            self._apply_parcel_event(event, payload)
            if event in _CALENDAR_EVENTS:
                self._calendar_debouncer.async_schedule_call()

    def _note_sequence(self, message: SseMessage) -> None:
        """Track the server's sequence; a message at or below it is a repeat."""
        seq = int(message.id) if message.id and message.id.isdigit() else None
        if message.event in (READY_EVENT, RESYNC_EVENT):
            # The server's current sequence, which may also be lower (a reset database).
            self._max_seq = seq
            self._repeat = False
            return
        self._repeat = (
            seq is not None and self._max_seq is not None and seq <= self._max_seq
        )
        if seq is not None and not self._repeat:
            self._max_seq = seq

    def _fire_once(
        self, key: tuple[str, str], change: object, event_data: dict[str, Any]
    ) -> None:
        """Fire a bus event unless this exact change was fired already.

        A replayed message (its sequence already seen) and an event repeating the
        change last fired for the same parcel or source are both dropped, so an
        automation runs once per change however often the server repeats it.
        """
        fingerprint = json.dumps(change, sort_keys=True, default=str)
        if self._repeat or self._fired.get(key) == fingerprint:
            _LOGGER.debug("Not firing %s again for the same change", key[0])
            return
        self._fired[key] = fingerprint
        self.hass.bus.async_fire(key[0], event_data)

    def _apply_position(self, payload: dict[str, Any]) -> None:
        parcel_id = merge_position(self.positions, payload)
        if parcel_id is not None:
            async_dispatcher_send(
                self.hass,
                SIGNAL_LIVE_POSITION.format(self.config_entry.entry_id),
                parcel_id,
            )

    def _disabled_changed(self) -> None:
        """A source was enabled or disabled: the server shows or hides its data.

        Hidden parcels go at once; parcels shown again only come with a reload.
        """
        drop_hidden(self.data, self.positions)
        self._want_refresh()

    def _apply_source_event(self, payload: dict[str, Any]) -> None:
        source = source_of(payload)
        source_id = str(source.get("id") or "")
        if source_id not in self.data.sources:
            # A source this entry has not seen yet: pick it up with a resync.
            self._want_refresh()
        elif merge_source_state(self.data, payload):
            self._disabled_changed()
        self._fire_once(
            (f"{DOMAIN}_source_state_changed", source_id),
            [payload.get("old_state"), payload.get("new_state"), payload.get("detail")],
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
        if not source_of(payload).get("id"):
            return
        new, crossed = merge_source_snapshot(self.data, payload)
        if new:
            self._want_refresh()
        if crossed:
            self._disabled_changed()
        self._publish()

    def _apply_source_removed(self, payload: dict[str, Any]) -> None:
        """Drop a source the server deleted or merged, with its device.

        Its parcels lose the link; the registry sync then removes the device and
        reloads the entry, so parcels it owned come back on their new owner.
        """
        source = source_of(payload)
        source_id = str(source.get("id") or "")
        if not source_id:
            return
        # Read the device before the registry sync removes it.
        device_id = self.device_id_for_source(source_id)
        merge_source_removed(self.data, payload)
        merged_into = payload.get("merged_into")
        if isinstance(merged_into, str) and merged_into not in self.data.sources:
            self._want_refresh()
        self._fire_once(
            (f"{DOMAIN}_source_removed", source_id),
            merged_into,
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

    def _apply_order_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Patch or unlink the order on its parcels; no bus event."""
        if event_type == ORDER_DELETED_EVENT:
            changed = merge_order_deleted(self.data, payload)
        else:
            changed = merge_order_updated(self.data, payload)
            # The parcels' own items and pictures may have changed with it.
            self._want_refresh()
        if changed:
            self._publish()

    def open_returns(self) -> list[dict[str, Any]]:
        """The open returns, soonest return-by day first (those without one last)."""
        returns = self.data.returns.values() if self.data else ()
        return sorted(
            returns,
            key=lambda r: (r.get("return_by") is None, str(r.get("return_by") or "")),
        )

    def _apply_return_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Store or drop the return, then fire its bus event once per change."""
        ret = return_of(payload)
        if ret is None:
            return
        if not merge_return(self.data, ret):
            return
        return_id = str(ret["id"])
        parcel = self.data.parcels.get(str(ret.get("parcel_id") or "")) or {}
        # What makes this a new change: everything but the timestamps.
        change = {k: v for k, v in ret.items() if k not in ("created_at", "updated_at")}
        self._fire_once(
            (f"{DOMAIN}_{RETURN_EVENT_SUFFIXES[event_type]}", return_id),
            change,
            {
                **return_summary(ret),
                "return_id": return_id,
                "device_id": self.device_id_for_source(owner_source_id(parcel)),
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
        gone = event_type in GONE_EVENTS
        shown = gone or not hidden(self.data, parcel)
        merge_parcel(
            self.data,
            self.positions,
            event_type,
            parcel,
            include_history=self.include_history,
        )
        source_id = owner_source_id(parcel)
        if source_id is None and gone and previous is not None:
            # A removal's payload may no longer name the parcel's sources: the trigger still
            # belongs on the device of the source that had it, not on the hub.
            source_id = owner_source_id(previous)
        if source_id and source_id not in self.data.sources:
            self._want_refresh()
        if shown and event_type in SERVER_EVENT_SUFFIXES:
            self._fire_parcel_event(event_type, payload, parcel, source_id)
        self._publish()

    def _fire_parcel_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        parcel: dict[str, Any],
        source_id: str | None,
    ) -> None:
        parcel_id = str(parcel["id"])
        if event_type in GONE_EVENTS:
            # Gone: a later registration of the same parcel is news again.
            self._fired = {
                key: value for key, value in self._fired.items() if key[1] != parcel_id
            }
        suite = to_suite_parcel(
            parcel,
            include_history=self.include_history,
            carrier_names=self.data.carriers,
        )
        # Bus events land in the recorder: the bulky and personal parts (items with
        # prices, the order, history, the live block, the lookup) stay on the
        # tracker, which keeps them out of history (HA-025).
        for key in _EVENT_DROPPED_KEYS:
            suite.pop(key, None)
        changes = {k: v for k, v in payload.items() if k != "parcel"}
        live = parcel.get("live") if isinstance(parcel.get("live"), dict) else {}
        # What makes this a new change: the event's own fields plus the parcel's
        # status, times and courier progress.
        change = [
            changes,
            parcel.get("status"),
            parcel.get("delivered_at"),
            parcel.get("planned_from"),
            parcel.get("planned_to"),
            live.get("state"),
            live.get("stops_away"),
        ]
        self._fire_once(
            (f"{DOMAIN}_{SERVER_EVENT_SUFFIXES[event_type]}", parcel_id),
            change,
            {
                **suite,
                **changes,
                "device_id": self.device_id_for_source(source_id),
                "hub_device_id": self.device_id_for_source(None),
            },
        )

    @callback
    def async_schedule_reconnect_refresh(self) -> None:
        """Reload once a reconnect's replay is in, unless the server sends `resync`."""
        self._reconnect_debouncer.async_schedule_call()

    @callback
    def async_forget_parcel(self, parcel_id: str) -> None:
        """Drop a parcel the server no longer has (its detail route answered 404)."""
        if self.data is not None and self.data.parcels.pop(parcel_id, None):
            self.positions.pop(parcel_id, None)
            self._publish()

    async def async_shutdown(self) -> None:
        """Cancel the calendar and reconnect debouncers too."""
        await super().async_shutdown()
        self._calendar_debouncer.async_shutdown()
        self._reconnect_debouncer.async_shutdown()
