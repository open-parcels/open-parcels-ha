"""Applying event-stream changes to the data: live, and again after a REST reload.

These functions only change the data; firing bus events stays with the
coordinator. That split lets a reload re-apply the events that arrived while
its requests were in flight (their bus events already fired) instead of
replacing them with an older snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    BACKUP_FINISHED_EVENT,
    CLOSED_RETURN_STATUSES,
    CONNECTION_REMOVED_EVENT,
    CONNECTION_STATE_EVENT,
    JOB_EVENTS,
    LIVE_POSITION_EVENT,
    LIVE_TRACKING_STATES,
    ORDER_CREATED_EVENT,
    ORDER_DELETED_EVENT,
    ORDER_UPDATED_EVENT,
    RETURN_EVENT_SUFFIXES,
    ConnectionState,
)

# Parcel events after which the parcel is gone from the server's lists.
GONE_EVENTS = frozenset({"parcel.removed", "parcel.deleted"})


@dataclass(slots=True)
class CourierPosition:
    """The courier's current position; kept in memory only."""

    latitude: float
    longitude: float
    accuracy: float | None
    position_at: str | None
    stops_away: int | None


@dataclass(slots=True)
class OpenParcelsData:
    """Everything the entities read, as last received from the server."""

    connections: dict[str, dict[str, Any]] = field(default_factory=dict)
    carriers: dict[str, str] = field(default_factory=dict)
    parcels: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Open returns by id (refunded, rejected, cancelled and expired ones are dropped).
    returns: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Open orders by id, as GET /orders and the order events give them (total, status).
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    # GET /status: the overall state, one state per part, and the parts' details.
    status: dict[str, Any] = field(default_factory=dict)
    # The newest backup attempt (ok, reason, at, file, bytes, error); None before the first.
    last_backup: dict[str, Any] | None = None
    # Each provider's actions (GET /carriers), and each connection's newest job.
    actions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Words in Home Assistant's language (texts.py), read on every full load.
    texts: dict[str, str] = field(default_factory=dict)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def courier_position(values: Mapping[str, Any]) -> CourierPosition | None:
    """A courier position from a live block or a position message; None without one."""
    lat, lng = _number(values.get("latitude")), _number(values.get("longitude"))
    if lat is None or lng is None:
        return None
    stops = values.get("stops_away")
    return CourierPosition(
        latitude=lat,
        longitude=lng,
        accuracy=_number(values.get("accuracy")),
        position_at=values.get("position_at"),
        stops_away=stops if isinstance(stops, int) else None,
    )


def position_from_live(live: Any) -> CourierPosition | None:
    """The courier position of a parcel's live block while its session runs."""
    if not isinstance(live, Mapping) or live.get("state") not in LIVE_TRACKING_STATES:
        return None
    return courier_position(live)


def older(parcel: Mapping[str, Any], stored: Mapping[str, Any] | None) -> bool:
    """Whether an event's parcel is older than the stored one, by `updated_at`."""
    if stored is None:
        return False
    new = dt_util.parse_datetime(str(parcel.get("updated_at") or ""))
    old = dt_util.parse_datetime(str(stored.get("updated_at") or ""))
    return new is not None and old is not None and new < old


def connection_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The `connection` object of a connection event; empty when missing or malformed."""
    connection = payload.get("connection")
    return connection if isinstance(connection, dict) else {}


def hidden(data: OpenParcelsData, parcel: Mapping[str, Any]) -> bool:
    """Whether the server hides the parcel: every connection it came from is disabled.

    A parcel without connections always shows; one naming a connection this entry does
    not know is kept until a reload settles it.
    """
    ids = [
        str(ref.get("connection_id"))
        for ref in parcel.get("connections") or []
        if isinstance(ref, Mapping) and ref.get("connection_id")
    ]
    return bool(ids) and all(
        (data.connections.get(sid) or {}).get("state") == ConnectionState.DISABLED
        for sid in ids
    )


def drop_hidden(data: OpenParcelsData, positions: dict[str, CourierPosition]) -> None:
    """Forget the parcels a disabled connection hides."""
    for parcel_id, parcel in list(data.parcels.items()):
        if hidden(data, parcel):
            data.parcels.pop(parcel_id)
            positions.pop(parcel_id, None)


def merge_parcel(
    data: OpenParcelsData,
    positions: dict[str, CourierPosition],
    event_type: str,
    parcel: dict[str, Any],
    *,
    include_history: bool,
) -> None:
    """Store the parcel an event carries, or drop it when the event says it is gone."""
    parcel_id = str(parcel["id"])
    previous = data.parcels.get(parcel_id)
    if event_type in GONE_EVENTS or hidden(data, parcel):
        data.parcels.pop(parcel_id, None)
        positions.pop(parcel_id, None)
        return
    if older(parcel, previous):
        # Older than what is stored (a replay, or a reload that already had it).
        return
    if not include_history and previous is not None and "history" not in parcel:
        parcel = {**parcel, "history": previous.get("history", [])}
    data.parcels[parcel_id] = parcel
    live = parcel.get("live")
    if not isinstance(live, Mapping) or live.get("state") not in LIVE_TRACKING_STATES:
        positions.pop(parcel_id, None)


def merge_position(
    positions: dict[str, CourierPosition], payload: Mapping[str, Any]
) -> str | None:
    """Take a courier position message; returns the parcel id it is for."""
    parcel_id = payload.get("parcel_id")
    if not isinstance(parcel_id, str):
        return None
    position = courier_position(payload)
    if position is None:
        # Not sent today (the server publishes positions with a fix only), kept so a
        # fix-less message can never leave a stale marker on the map.
        positions.pop(parcel_id, None)
    else:
        positions[parcel_id] = position
    return parcel_id


def _crosses_disabled(old: Any, new: Any) -> bool:
    return old != new and ConnectionState.DISABLED in (old, new)


def merge_connection_state(data: OpenParcelsData, payload: Mapping[str, Any]) -> bool:
    """Apply `connection.state_changed`; True when it enabled or disabled the connection."""
    known = data.connections.get(str(connection_of(payload).get("id") or ""))
    if known is None:
        return False
    old = known.get("state")
    known["state"] = payload.get("new_state", old)
    known["state_reason"] = payload.get("state_reason")
    known["carrier_message"] = payload.get("carrier_message")
    return _crosses_disabled(old, known["state"])


def merge_connection_snapshot(
    data: OpenParcelsData, payload: Mapping[str, Any]
) -> tuple[bool, bool]:
    """Take a connection as an event reports it.

    Returns whether it is new to this entry and whether it was enabled or disabled.
    """
    connection = connection_of(payload)
    connection_id = str(connection.get("id") or "")
    if not connection_id:
        return False, False
    known = data.connections.get(connection_id)
    new = known is None
    if known is None:
        known = data.connections[connection_id] = {"id": connection_id}
    old = known.get("state")
    for key in ("provider", "label", "kind"):
        if key in connection:
            known[key] = connection[key]
    if "state" in payload:
        known["state"] = payload["state"]
        known["state_reason"] = payload.get("state_reason")
        known["carrier_message"] = payload.get("carrier_message")
    return new, _crosses_disabled(old, known.get("state")) and not new


def merge_connection_removed(data: OpenParcelsData, payload: Mapping[str, Any]) -> None:
    """Drop a deleted or merged connection; its parcels lose the link."""
    connection_id = str(connection_of(payload).get("id") or "")
    data.connections.pop(connection_id, None)
    for pid, parcel in list(data.parcels.items()):
        refs = parcel.get("connections") or []
        kept = [
            ref
            for ref in refs
            if not (
                isinstance(ref, Mapping) and ref.get("connection_id") == connection_id
            )
        ]
        if len(kept) != len(refs):
            data.parcels[pid] = {**parcel, "connections": kept}


def merge_order_updated(data: OpenParcelsData, payload: Mapping[str, Any]) -> bool:
    """Patch the order on the parcels that carry it; True when any did."""
    order_id = payload.get("id")
    changed = False
    for pid, parcel in list(data.parcels.items()):
        order = parcel.get("order")
        if not isinstance(order, Mapping) or order.get("id") != order_id:
            continue
        patched = dict(order)
        for key in ("shop", "order_number", "items"):
            if key in payload:
                patched[key] = payload[key]
        data.parcels[pid] = {**parcel, "order": patched}
        changed = True
    return changed


def merge_order(data: OpenParcelsData, payload: Mapping[str, Any]) -> None:
    """Keep an order from `order.created` / `order.updated`; an archived one is no longer open."""
    order_id = payload.get("id")
    if not isinstance(order_id, str) or not order_id:
        return
    if payload.get("archived"):
        data.orders.pop(order_id, None)
        return
    data.orders[order_id] = {k: v for k, v in payload.items() if k != "parcels"}


def backup_from_status(status: Mapping[str, Any]) -> dict[str, Any] | None:
    """The newest backup as GET /status sums it up (without the file); None before one."""
    backups = status.get("backups")
    if not isinstance(backups, Mapping) or backups.get("last_at") is None:
        return None
    return {
        "ok": backups.get("last_ok"),
        "reason": None,
        "at": backups.get("last_at"),
        "file": None,
        "bytes": None,
        "error": backups.get("last_error"),
    }


def merge_backup(data: OpenParcelsData, payload: Mapping[str, Any]) -> bool:
    """Take a `backup.finished` result unless an equal or newer one is kept; True if taken."""
    at = payload.get("at")
    if not isinstance(at, str):
        return False
    last = data.last_backup
    if last is not None and str(last.get("at") or "") > at:
        return False
    data.last_backup = {
        key: payload.get(key)
        for key in ("ok", "reason", "at", "file", "bytes", "error")
    }
    return True


def merge_order_deleted(data: OpenParcelsData, payload: Mapping[str, Any]) -> bool:
    """Unlink the parcels of a deleted order; True when any carried it."""
    order_id = payload.get("id")
    if isinstance(order_id, str):
        data.orders.pop(order_id, None)
    listed = payload.get("parcel_ids")
    parcel_ids = set(listed) if isinstance(listed, list) else set()
    changed = False
    for pid, parcel in list(data.parcels.items()):
        order = parcel.get("order")
        if not isinstance(order, Mapping):
            continue
        if pid in parcel_ids or order.get("id") == order_id:
            data.parcels[pid] = {**parcel, "order": None}
            changed = True
    for return_id, ret in list(data.returns.items()):
        if ret.get("order_id") == order_id:
            data.returns.pop(return_id)
            changed = True
    return changed


def return_of(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `return` object of a return event; None when missing or malformed."""
    ret = payload.get("return")
    return ret if isinstance(ret, dict) and ret.get("id") else None


def merge_return(data: OpenParcelsData, ret: dict[str, Any]) -> bool:
    """Store an open return, or drop a closed one; False when the event is stale."""
    return_id = str(ret["id"])
    if older(ret, data.returns.get(return_id)):
        return False
    if ret.get("status") in CLOSED_RETURN_STATUSES:
        data.returns.pop(return_id, None)
    else:
        data.returns[return_id] = ret
    return True


def merge_job(data: OpenParcelsData, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the job a live event carries as its connection's newest; None when stale."""
    job = payload.get("job")
    if not isinstance(job, dict) or not job.get("connection_id"):
        return None
    connection_id = str(job["connection_id"])
    current = data.jobs.get(connection_id)
    if current is not None and current.get("id") != job.get("id"):
        # Another job: only a newer one replaces it.
        if str(job.get("created_at") or "") < str(current.get("created_at") or ""):
            return None
    elif current is not None and str(job.get("updated_at") or "") < str(
        current.get("updated_at") or ""
    ):
        return None
    data.jobs[connection_id] = job
    return job


def reapply(
    data: OpenParcelsData,
    positions: dict[str, CourierPosition],
    event_type: str,
    payload: dict[str, Any],
    *,
    include_history: bool,
) -> None:
    """Apply a message again to a newer snapshot, without any side effects."""
    if event_type == LIVE_POSITION_EVENT:
        merge_position(positions, payload)
    elif event_type == CONNECTION_STATE_EVENT:
        if merge_connection_state(data, payload):
            drop_hidden(data, positions)
    elif event_type == CONNECTION_REMOVED_EVENT:
        merge_connection_removed(data, payload)
    elif event_type.startswith("connection."):
        merge_connection_snapshot(data, payload)
        drop_hidden(data, positions)
    elif event_type in (ORDER_CREATED_EVENT, ORDER_UPDATED_EVENT):
        merge_order(data, payload)
        merge_order_updated(data, payload)
    elif event_type == BACKUP_FINISHED_EVENT:
        merge_backup(data, payload)
    elif event_type in JOB_EVENTS:
        merge_job(data, payload)
    elif event_type == ORDER_DELETED_EVENT:
        merge_order_deleted(data, payload)
    elif event_type in RETURN_EVENT_SUFFIXES:
        if (ret := return_of(payload)) is not None:
            merge_return(data, ret)
    elif isinstance(parcel := payload.get("parcel"), dict) and parcel.get("id"):
        merge_parcel(
            data, positions, event_type, parcel, include_history=include_history
        )


def loaded_backup(
    backups: dict[str, Any] | None, status: dict[str, Any]
) -> dict[str, Any] | None:
    """The newest backup: GET /data/backups' when the token may read it, else the status's."""
    if backups is None:
        return backup_from_status(status)
    last = backups.get("last")
    if not isinstance(last, dict):
        return None
    return {
        key: last.get(key) for key in ("ok", "reason", "at", "file", "bytes", "error")
    }


def order_summary(order: dict[str, Any]) -> dict[str, Any]:
    """An order for the bus: no items with their prices (HA-025), just how many."""
    items = order.get("items")
    return {
        "order_id": order.get("id"),
        "shop": order.get("shop"),
        "order_number": order.get("order_number"),
        "display_name": order.get("display_name"),
        "status": order.get("status"),
        "placed_at": order.get("placed_at"),
        "digital": order.get("digital"),
        "total": order.get("total"),
        "item_count": len(items) if isinstance(items, list) else 0,
    }
