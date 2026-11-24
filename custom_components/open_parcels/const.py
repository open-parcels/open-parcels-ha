"""Constants for the Open Parcels integration."""

from __future__ import annotations

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "open_parcels"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status, identical to the server's and the suite's list."""

    REGISTERED = "registered"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    AT_PICKUP_POINT = "at_pickup_point"
    DELIVERED = "delivered"
    RETURNING = "returning"
    PROBLEM = "problem"
    UNKNOWN = "unknown"


# Statuses of a parcel still on its way (or waiting to be fetched or sent back): only
# these get their own tracker entity. Finished and unknown parcels show up in the list
# sensors instead, so a connection's device page does not fill up with old deliveries.
OPEN_STATUSES = frozenset(
    {
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
        ParcelStatus.PROBLEM,
    }
)


class ConnectionState(StrEnum):
    """State of an Open Parcels connection."""

    OK = "ok"
    NEEDS_SIGN_IN = "needs_sign_in"
    ERROR = "error"
    DISABLED = "disabled"
    # The last run deliberately did nothing; the detail says why.
    IDLE = "idle"


CONF_API_TOKEN = "api_token"

# Options. The delivered filter is the suite's, unchanged.
CONF_RESYNC_MINUTES = "resync_minutes"
DEFAULT_RESYNC_MINUTES = 30
MIN_RESYNC_MINUTES = 5
MAX_RESYNC_MINUTES = 1440
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False
# One sensor per open order (its items and parcels); off by default, like the parcels' list.
CONF_ORDER_ENTITIES = "order_entities"
DEFAULT_ORDER_ENTITIES = False
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Keep each parcel's history attribute well under HA's ~16 KB attribute limit.
HISTORY_MAX_EVENTS = 20

# Page size for the parcel list (the server's maximum); further pages follow X-Next-Offset.
PARCEL_LIST_LIMIT = 1000

# Calendar cache window used for the entity's "next event" state.
CALENDAR_PAST_DAYS = 1
CALENDAR_FUTURE_DAYS = 60

# Event stream reconnect backoff.
STREAM_BACKOFF_INITIAL_SECONDS = 1.0
STREAM_BACKOFF_MAX_SECONDS = 300.0
# The server sends a heartbeat every 25 s; three missed ones mean the link is dead.
STREAM_READ_TIMEOUT_SECONDS = 90.0

# Server event type -> Home Assistant bus event suffix (fired as f"{DOMAIN}_{suffix}").
# Names follow the suite's contract, so automations only swap the prefix.
SERVER_EVENT_SUFFIXES: dict[str, str] = {
    "parcel.registered": "parcel_registered",
    "parcel.status_changed": "parcel_status_changed",
    "parcel.delivered": "parcel_delivered",
    "parcel.delivery_time_changed": "parcel_delivery_time_changed",
    "parcel.sending_registered": "sending_parcel_registered",
    "parcel.sending_delivered": "sending_parcel_delivered",
    "parcel.live_updated": "parcel_live_updated",
    "parcel.removed": "parcel_removed",
    "parcel.deleted": "parcel_deleted",
    "connection.state_changed": "connection_state_changed",
}
LIVE_POSITION_EVENT = "parcel.live_position"

# Connection removed (deleted, or merged into another): fired on the bus, but not a device
# trigger, because the device it would belong to is removed with it.
CONNECTION_REMOVED_EVENT = "connection.removed"
# Events carrying a connection as it now is (label, state, detail): a new one gets its
# device, a renamed one its new device name. Not device triggers.
CONNECTION_SNAPSHOT_EVENTS = frozenset(
    {"connection.added", "connection.updated", "connection.run_finished"}
)
CONNECTION_STATE_EVENT = "connection.state_changed"
# A parcel's place changed (scans placed by their place names): taken as a parcel
# snapshot, not fired on the bus.
PARCEL_LOCATION_EVENT = "parcel.location_changed"
# Orders: patched onto the parcels that carry them, not fired on the bus.
ORDER_UPDATED_EVENT = "order.updated"
ORDER_DELETED_EVENT = "order.deleted"
# Returns: kept in the returns sensor and fired on the bus as f"{DOMAIN}_{suffix}".
RETURN_EVENT_SUFFIXES: dict[str, str] = {
    "return.created": "return_created",
    "return.updated": "return_updated",
}
# Return statuses after which a return is no longer open (the server's `closed` view).
CLOSED_RETURN_STATUSES = frozenset({"refunded", "rejected", "cancelled", "expired"})
# After a reconnect that replayed (no `resync`), one REST reload this long after, in
# case the server pruned short-lived events from the gap without saying so.
RECONNECT_REFRESH_DELAY_SECONDS = 30.0
# Stream control messages: both carry the server's current sequence as their id.
# `ready` opens a stream sent without Last-Event-ID; `resync` says the id sent
# could not be replayed (too old, or the server's database was reset).
READY_EVENT = "ready"
RESYNC_EVENT = "resync"
# A server setting changed (how parcels are named, the home location): read everything again.
SETTINGS_UPDATED_EVENT = "settings.updated"

# Dispatcher signal for courier positions, formatted with the entry id.
SIGNAL_LIVE_POSITION = f"{DOMAIN}_live_position_{{}}"

# Parcel pictures (item grids) are served by Home Assistant itself, so the frontend can load
# them without the server's token: {entry_id}/{parcel_id}, signed.
PICTURE_URL = "/api/open_parcels/picture/{entry_id}/{parcel_id}"

# Live session states during which the courier's position is shown: until the parcel is
# delivered (or the session ends), the next stop included.
LIVE_TRACKING_STATES = frozenset({"en_route", "next_stop"})

ATTRIBUTION = "Data provided by Open Parcels"
