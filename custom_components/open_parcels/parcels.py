"""Pure helpers that turn server parcels into the suite's parcel shape.

No I/O and no Home Assistant objects, so all of it is unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
import hashlib
from typing import Any

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

# Courier coordinates never reach attributes or events: the device tracker is the
# only place a position appears, and it is kept out of the recorder.
_LIVE_COORDINATE_KEYS = ("latitude", "longitude", "accuracy")


def parse_iso(value: Any) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime; naive values are UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def effective_status(parcel: Mapping[str, Any]) -> ParcelStatus:
    """Return the manual status when set, else the carrier's, as a ParcelStatus."""
    value = parcel.get("manual_status") or parcel.get("status")
    try:
        return ParcelStatus(str(value))
    except ValueError:
        return ParcelStatus.UNKNOWN


def is_delivered(parcel: Mapping[str, Any]) -> bool:
    """Whether the parcel counts as delivered (a manual status wins)."""
    return effective_status(parcel) is ParcelStatus.DELIVERED or (
        not parcel.get("manual_status") and bool(parcel.get("delivered"))
    )


def owner_connection_id(parcel: Mapping[str, Any]) -> str | None:
    """The connection whose device the parcel's entities live on.

    The primary connection wins, then a hand-off, then an email connection (role `mail`). A parcel no
    connection follows (tracked by code only) belongs to the hub.
    """
    refs = [ref for ref in parcel.get("connections") or [] if isinstance(ref, Mapping)]
    for role in ("primary", "handoff", "mail"):
        for ref in refs:
            if ref.get("role") == role and ref.get("connection_id"):
                return str(ref["connection_id"])
    return None


def strip_live(live: Any) -> dict[str, Any] | None:
    """Return the live block without courier coordinates."""
    if not isinstance(live, Mapping):
        return None
    return {k: v for k, v in live.items() if k not in _LIVE_COORDINATE_KEYS}


def _history(entries: Any) -> list[dict[str, Any]]:
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        moment = parse_iso(entry.get("timestamp"))
        if moment is None:
            continue
        parsed.append(
            (
                moment,
                {
                    "timestamp": entry.get("timestamp"),
                    "status": entry.get("status"),
                    "raw_status": entry.get("raw_status"),
                    "location": entry.get("location"),
                },
            )
        )
    parsed.sort(key=lambda item: item[0])
    return [item for _, item in parsed][-HISTORY_MAX_EVENTS:]


def to_suite_parcel(
    parcel: Mapping[str, Any],
    *,
    include_history: bool,
    carrier_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the suite's parcel dict, followed by this service's additions.

    The suite keys come first, in the suite's order, and are never omitted.
    ``carrier`` is the carrier's display name (as the suite publishes it);
    ``carrier_id`` keeps the server's identifier.
    """
    status = effective_status(parcel)
    delivered = is_delivered(parcel)
    carrier_id = str(parcel.get("carrier") or "")
    names = carrier_names or {}
    order = parcel.get("order")
    return {
        "carrier": names.get(carrier_id, carrier_id) or None,
        "tracking_number": parcel.get("tracking_number"),
        "sender": parcel.get("sender"),
        "receiver": parcel.get("receiver"),
        "status": status.value,
        "raw_status": parcel.get("raw_status"),
        "delivered": delivered,
        "delivered_at": parcel.get("delivered_at") if delivered else None,
        "expected_from": None if delivered else parcel.get("expected_from"),
        "expected_to": None if delivered else parcel.get("expected_to"),
        # The expected delivery as the server's local days, when it is whole days.
        "expected_days": None if delivered else parcel.get("expected_days"),
        "pickup": bool(parcel.get("pickup")),
        "pickup_point": parcel.get("pickup_point"),
        "url": parcel.get("url"),
        "weight": None,
        "dimensions": None,
        "history": _history(parcel.get("history")) if include_history else None,
        "raw": None,
        "id": parcel.get("id"),
        "carrier_id": carrier_id or None,
        "direction": parcel.get("direction") or "receiving",
        "label": parcel.get("label"),
        # The label, else the item names or the code, per the server's setting.
        "display_name": parcel.get("display_name")
        or parcel.get("label")
        or parcel.get("tracking_number"),
        # The server's `status` is already the effective one.
        "carrier_status": parcel.get("carrier_status"),
        "manual_status": parcel.get("manual_status"),
        "items": list(parcel.get("items") or []),
        "order": dict(order) if isinstance(order, Mapping) else None,
        "live": strip_live(parcel.get("live")),
        "lookup": parcel.get("lookup"),
        "connection_id": owner_connection_id(parcel),
        "first_seen_at": parcel.get("first_seen_at"),
        "updated_at": parcel.get("updated_at"),
    }


def order_key(shop: str, order_number: str) -> str:
    """A stable, short key for an order (shop and number), for unique ids."""
    return hashlib.sha256(f"{shop}\0{order_number}".encode()).hexdigest()[:16]


def sort_parcels_by_ts(
    parcels: Iterable[dict[str, Any]], key_field: str, *, descending: bool = False
) -> list[dict[str, Any]]:
    """Sort by the timestamp at ``key_field``; missing values always go last."""
    with_ts: list[tuple[datetime, dict[str, Any]]] = []
    without_ts: list[dict[str, Any]] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(
    parcels: list[dict[str, Any]],
    options: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Trim a newest-first delivered list per the suite's retention option."""
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = (now or datetime.now(UTC)) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (moment := parse_iso(parcel.get("delivered_at"))) is None
            or moment >= cutoff
        ]
    return parcels[:amount]


def count_by_carrier(parcels: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count parcels per carrier display name."""
    counts: dict[str, int] = {}
    for parcel in parcels:
        label = parcel.get("carrier") or "Unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts


def next_delivery(
    parcels: Iterable[dict[str, Any]],
) -> tuple[datetime | None, dict[str, Any] | None, dict[str, datetime]]:
    """Earliest ``expected_from`` overall, its parcel, and the earliest per carrier."""
    earliest: datetime | None = None
    earliest_parcel: dict[str, Any] | None = None
    by_carrier: dict[str, datetime] = {}
    for parcel in parcels:
        moment = parse_iso(parcel.get("expected_from"))
        if moment is None:
            continue
        label = parcel.get("carrier") or "Unknown"
        if label not in by_carrier or moment < by_carrier[label]:
            by_carrier[label] = moment
        if earliest is None or moment < earliest:
            earliest = moment
            earliest_parcel = parcel
    return earliest, earliest_parcel, by_carrier
