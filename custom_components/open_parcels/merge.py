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
    LIVE_POSITION_EVENT,
    LIVE_TRACKING_STATES,
    ORDER_DELETED_EVENT,
    ORDER_UPDATED_EVENT,
    SOURCE_REMOVED_EVENT,
    SOURCE_STATE_EVENT,
    SourceState,
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

    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    carriers: dict[str, str] = field(default_factory=dict)
    parcels: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        # Newer servers send `position_at`, older ones `positionAt`.
        position_at=values.get("position_at") or values.get("positionAt"),
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


def source_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The `source` object of a source event; empty when missing or malformed."""
    source = payload.get("source")
    return source if isinstance(source, dict) else {}


def hidden(data: OpenParcelsData, parcel: Mapping[str, Any]) -> bool:
    """Whether the server hides the parcel: every source it came from is disabled.

    A parcel without sources always shows; one naming a source this entry does
    not know is kept until a reload settles it.
    """
    ids = [
        str(ref.get("source_id"))
        for ref in parcel.get("sources") or []
        if isinstance(ref, Mapping) and ref.get("source_id")
    ]
    return bool(ids) and all(
        (data.sources.get(sid) or {}).get("state") == SourceState.DISABLED
        for sid in ids
    )


def drop_hidden(data: OpenParcelsData, positions: dict[str, CourierPosition]) -> None:
    """Forget the parcels a disabled source hides."""
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
        positions.pop(parcel_id, None)
    else:
        positions[parcel_id] = position
    return parcel_id


def _crosses_disabled(old: Any, new: Any) -> bool:
    return old != new and SourceState.DISABLED in (old, new)


def merge_source_state(data: OpenParcelsData, payload: Mapping[str, Any]) -> bool:
    """Apply `source.state_changed`; True when it enabled or disabled the source."""
    known = data.sources.get(str(source_of(payload).get("id") or ""))
    if known is None:
        return False
    old = known.get("state")
    known["state"] = payload.get("new_state", old)
    known["state_detail"] = payload.get("detail")
    return _crosses_disabled(old, known["state"])


def merge_source_snapshot(
    data: OpenParcelsData, payload: Mapping[str, Any]
) -> tuple[bool, bool]:
    """Take a source as an event reports it.

    Returns whether it is new to this entry and whether it was enabled or disabled.
    """
    source = source_of(payload)
    source_id = str(source.get("id") or "")
    if not source_id:
        return False, False
    known = data.sources.get(source_id)
    new = known is None
    if known is None:
        known = data.sources[source_id] = {"id": source_id}
    old = known.get("state")
    for key in ("plugin", "label", "kind"):
        if key in source:
            known[key] = source[key]
    if "state" in payload:
        known["state"] = payload["state"]
        known["state_detail"] = payload.get("detail")
    return new, _crosses_disabled(old, known.get("state")) and not new


def merge_source_removed(data: OpenParcelsData, payload: Mapping[str, Any]) -> None:
    """Drop a deleted or merged source; its parcels lose the link."""
    source_id = str(source_of(payload).get("id") or "")
    data.sources.pop(source_id, None)
    for pid, parcel in list(data.parcels.items()):
        refs = parcel.get("sources") or []
        kept = [
            ref
            for ref in refs
            if not (isinstance(ref, Mapping) and ref.get("source_id") == source_id)
        ]
        if len(kept) != len(refs):
            data.parcels[pid] = {**parcel, "sources": kept}


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


def merge_order_deleted(data: OpenParcelsData, payload: Mapping[str, Any]) -> bool:
    """Unlink the parcels of a deleted order; True when any carried it."""
    order_id = payload.get("id")
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
    return changed


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
    elif event_type == SOURCE_STATE_EVENT:
        if merge_source_state(data, payload):
            drop_hidden(data, positions)
    elif event_type == SOURCE_REMOVED_EVENT:
        merge_source_removed(data, payload)
    elif event_type.startswith("source."):
        merge_source_snapshot(data, payload)
        drop_hidden(data, positions)
    elif event_type == ORDER_UPDATED_EVENT:
        merge_order_updated(data, payload)
    elif event_type == ORDER_DELETED_EVENT:
        merge_order_deleted(data, payload)
    elif isinstance(parcel := payload.get("parcel"), dict) and parcel.get("id"):
        merge_parcel(
            data, positions, event_type, parcel, include_history=include_history
        )
