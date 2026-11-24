"""Pure parcel helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.open_parcels.const import ParcelStatus
from custom_components.open_parcels.parcels import (
    apply_delivered_filter,
    count_by_carrier,
    effective_status,
    is_delivered,
    next_delivery,
    owner_connection_id,
    parse_iso,
    sort_parcels_by_ts,
    strip_live,
    to_suite_parcel,
)

from . import payloads


def test_parse_iso() -> None:
    """Naive values are UTC; junk is None."""
    assert parse_iso("2030-01-01T00:00:00") == datetime(2030, 1, 1, tzinfo=UTC)
    assert parse_iso("junk") is None
    assert parse_iso(None) is None


def test_manual_status_wins() -> None:
    """A manual status overrides the carrier's, also for delivered."""
    assert effective_status({"status": "in_transit", "manual_status": "delivered"}) is (
        ParcelStatus.DELIVERED
    )
    assert effective_status({"status": "weird"}) is ParcelStatus.UNKNOWN
    assert is_delivered({"status": "in_transit", "manual_status": "delivered"})
    assert not is_delivered(
        {"status": "delivered", "delivered": True, "manual_status": "in_transit"}
    )
    parcel = to_suite_parcel(
        {**payloads.parcels()[0], "manual_status": "delivered"}, include_history=False
    )
    assert parcel["delivered"] is True
    assert parcel["expected_from"] is None
    assert parcel["carrier_status"] == "out_for_delivery"


def test_glossary_names_reach_the_suite_parcel() -> None:
    """The server's tracking_number, expected_* and expected_days become attributes."""
    days = {"from": "2030-01-02", "to": "2030-01-03"}
    parcel = to_suite_parcel(
        {**payloads.parcels()[0], "expected_days": days, "direction": "sending"},
        include_history=False,
    )
    assert parcel["tracking_number"] == "TEST0000000001"
    assert parcel["expected_from"] == "2030-01-02T12:00:00.000Z"
    assert parcel["expected_days"] == days
    assert parcel["direction"] == "sending"
    assert "barcode" not in parcel
    assert "planned_from" not in parcel


def test_owner_connection_prefers_primary() -> None:
    """Primary beats hand-off beats mail; none means the hub."""
    refs = [
        {"connection_id": "m", "role": "mail"},
        {"connection_id": "h", "role": "handoff"},
        {"connection_id": "p", "role": "primary"},
    ]
    assert owner_connection_id({"connections": refs}) == "p"
    assert owner_connection_id({"connections": refs[:2]}) == "h"
    assert owner_connection_id({"connections": refs[:1]}) == "m"
    assert owner_connection_id({"connections": ["junk"]}) is None


def test_strip_live() -> None:
    """Coordinates go, the rest stays."""
    assert strip_live(None) is None
    assert strip_live({"state": "en_route", "latitude": 1, "longitude": 2}) == {
        "state": "en_route"
    }


def test_history_skips_junk_and_caps() -> None:
    """Entries without a timestamp are dropped; the newest 20 are kept."""
    history = [
        {"timestamp": f"2030-01-01T00:{minute:02d}:00Z", "raw_status": str(minute)}
        for minute in range(30)
    ]
    history += ["junk", {"timestamp": "bad"}]
    parcel = to_suite_parcel(
        {**payloads.parcels()[1], "history": history}, include_history=True
    )
    assert len(parcel["history"]) == 20
    assert parcel["history"][-1]["raw_status"] == "29"


def test_sorting_and_filters() -> None:
    """Missing timestamps sort last; filters by days and count."""
    items = [{"t": None}, {"t": "2030-01-02T00:00:00Z"}, {"t": "2030-01-01T00:00:00Z"}]
    assert [i["t"] for i in sort_parcels_by_ts(items, "t")] == [
        "2030-01-01T00:00:00Z",
        "2030-01-02T00:00:00Z",
        None,
    ]
    delivered = [{"delivered_at": "2030-01-10T00:00:00Z"}, {"delivered_at": None}]
    now = datetime(2030, 1, 20, tzinfo=UTC)
    assert apply_delivered_filter(
        delivered, {"delivered_filter_amount": 5}, now=now
    ) == [{"delivered_at": None}]
    assert (
        apply_delivered_filter(
            delivered,
            {"delivered_filter_type": "parcels", "delivered_filter_amount": 1},
        )
        == delivered[:1]
    )


def test_counts_and_next_delivery() -> None:
    """Per-carrier counts and earliest expected_from."""
    parcels = [
        {"carrier": "DHL", "expected_from": "2030-01-03T00:00:00Z"},
        {"carrier": "DHL", "expected_from": "2030-01-02T00:00:00Z"},
        {"carrier": None, "expected_from": None},
    ]
    assert count_by_carrier(parcels) == {"DHL": 2, "Unknown": 1}
    value, parcel, by_carrier = next_delivery(parcels)
    assert value == datetime(2030, 1, 2, tzinfo=UTC)
    assert parcel is parcels[1]
    assert by_carrier == {"DHL": value}
