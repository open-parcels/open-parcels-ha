"""Made-up server payloads. Every value here is invented."""

from __future__ import annotations

import copy
from typing import Any

CONNECTION_DHL = "11111111-1111-4111-8111-111111111111"
CONNECTION_GLS = "22222222-2222-4222-8222-222222222222"
PARCEL_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # dhl, out for delivery, live
PARCEL_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"  # gls, at pickup point
PARCEL_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"  # dhl, delivered
PARCEL_D = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"  # no connection, sending

CARRIERS: list[dict[str, Any]] = [
    {
        "id": carrier_id,
        "name": name,
        "countries": ["de"],
        "connection_kinds": ["account", "tracking"],
        "sign_in": "none",
        "sign_in_flows": [],
        "config_schema": [],
        "secret_schema": [],
    }
    for carrier_id, name in (("dhl", "DHL"), ("gls", "GLS"))
]

CONNECTIONS: list[dict[str, Any]] = [
    {
        "id": CONNECTION_DHL,
        "provider": "dhl",
        "kind": "account",
        "label": "Germany (abc123)",
        "config": {"country": "de"},
        "has_secret": True,
        "state": "ok",
        "state_detail": None,
        "next_run_at": "2030-01-02T10:00:00.000Z",
        "last_run_at": "2030-01-02T09:00:00.000Z",
        "last_ok_at": "2030-01-02T09:00:00.000Z",
    },
    {
        "id": CONNECTION_GLS,
        "provider": "gls",
        "kind": "tracking",
        "label": "GLS (99999)",
        "config": {},
        "has_secret": False,
        "state": "needs_sign_in",
        "state_detail": "Sign in again",
        "next_run_at": None,
        "last_run_at": None,
        "last_ok_at": None,
    },
]


def _parcel(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "carrier": "dhl",
        "tracking_number": "TEST0000000001",
        "sender": "Example Shop",
        "receiver": "Jane Example",
        "status": "in_transit",
        "raw_status": "On its way",
        "delivered": False,
        "delivered_at": None,
        "expected_from": None,
        "expected_to": None,
        "pickup": False,
        "pickup_point": None,
        "history": [],
        "url": "https://track.example.test/TEST0000000001",
        "id": PARCEL_A,
        "direction": "receiving",
        "label": None,
        "manual_status": None,
        "items": [],
        "live": None,
        "location": None,
        "order": None,
        "connections": [],
        "lookup": {"state": "found", "since": None, "checked_at": None},
        "first_seen_at": "2030-01-01T08:00:00.000Z",
        "updated_at": "2030-01-02T08:00:00.000Z",
        "archived_at": None,
        "last_scan": None,
        "expected_days": None,
    }
    base.update(overrides)
    # Without a manual status the carrier's view matches the effective one.
    base.setdefault("carrier_status", base["status"])
    base.setdefault("carrier_delivered", base["delivered"])
    # The server always sends a display name; by default it is the tracking code.
    base.setdefault("display_name", base["tracking_number"])
    return base


def _ref(connection_id: str, provider: str, role: str = "primary") -> dict[str, Any]:
    return {
        "connection_id": connection_id,
        "provider": provider,
        "role": role,
        "kind": "account",
        "label": "Example account",
        "last_seen_at": "2030-01-02T08:00:00.000Z",
    }


def parcels(now_iso_expected: str = "2030-01-02T12:00:00.000Z") -> list[dict[str, Any]]:
    """Four parcels covering the interesting cases."""
    return copy.deepcopy(
        [
            _parcel(
                id=PARCEL_A,
                tracking_number="TEST0000000001",
                status="out_for_delivery",
                expected_from=now_iso_expected,
                expected_to="2030-01-02T14:00:00.000Z",
                connections=[_ref(CONNECTION_DHL, "dhl")],
                history=[
                    {
                        "timestamp": "2030-01-02T07:00:00.000Z",
                        "status": "out_for_delivery",
                        "raw_status": "With the courier",
                        "location": "Example Town",
                    },
                    {
                        "timestamp": "2030-01-01T07:00:00.000Z",
                        "status": "in_transit",
                        "raw_status": "Sorted",
                        "location": None,
                    },
                ],
                live={
                    "state": "en_route",
                    "stops_away": 3,
                    "latitude": 50.0,
                    "longitude": 8.0,
                    "accuracy": 15,
                    "position_at": "2030-01-02T09:30:00.000Z",
                    "message": None,
                    "updated_at": "2030-01-02T09:30:00.000Z",
                },
            ),
            _parcel(
                id=PARCEL_B,
                carrier="gls",
                tracking_number="TEST0000000002",
                status="at_pickup_point",
                pickup=True,
                pickup_point="Example Kiosk",
                connections=[_ref(CONNECTION_GLS, "gls")],
            ),
            _parcel(
                id=PARCEL_C,
                tracking_number="TEST0000000003",
                status="delivered",
                delivered=True,
                delivered_at="2999-01-01T00:00:00.000Z",
                expected_from="2030-01-01T10:00:00.000Z",
                connections=[_ref(CONNECTION_DHL, "dhl", "handoff")],
            ),
            _parcel(
                id=PARCEL_D,
                carrier="dhl",
                tracking_number="TEST0000000004",
                direction="sending",
                status="registered",
                connections=[],
            ),
        ]
    )


def calendar_entries() -> list[dict[str, Any]]:
    """Calendar entries as GET /calendar returns them."""
    return [
        {
            "uid": f"{PARCEL_A}@open-parcels",
            "parcel_id": PARCEL_A,
            "title": "Example Shop",
            "carrier": "DHL",
            "tracking_number": "TEST0000000001",
            "status": "out_for_delivery",
            "kind": "expected",
            "start": "2999-01-02T12:00:00.000Z",
            "end": "2999-01-02T14:00:00.000Z",
            "all_day": False,
            "late": False,
            "image_url": None,
            "url": "https://track.example.test/TEST0000000001",
            "pickup_point": None,
            "start_date": None,
            "end_date": None,
            "order_id": None,
            "return_id": None,
            "details": [],
        },
        {
            "uid": f"{PARCEL_B}@open-parcels",
            "parcel_id": PARCEL_B,
            "title": "Example Kiosk parcel",
            "carrier": "GLS",
            "tracking_number": "TEST0000000002",
            "status": "at_pickup_point",
            "kind": "expected",
            "start": "2999-01-05T00:00:00.000Z",
            "end": "2999-01-06T00:00:00.000Z",
            "all_day": True,
            "late": False,
            "image_url": None,
            "url": None,
            "pickup_point": "Example Kiosk",
            "start_date": "2999-01-05",
            "end_date": "2999-01-06",
            "order_id": None,
            "return_id": None,
            "details": [],
        },
        {
            "uid": f"{PARCEL_C}@open-parcels",
            "parcel_id": PARCEL_C,
            "title": "Delivered thing",
            "carrier": "DHL",
            "tracking_number": "TEST0000000003",
            "status": "delivered",
            "kind": "delivered",
            "start": "2999-01-01T00:00:00.000Z",
            "end": "2999-01-01T00:00:00.000Z",
            "all_day": False,
            "late": False,
            "image_url": None,
            "url": None,
            "pickup_point": None,
            "start_date": None,
            "end_date": None,
            "order_id": None,
            "return_id": None,
            "details": [],
        },
    ]


RETURN_OPEN = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
RETURN_ORDER = "ffffffff-0000-4fff-8fff-ffffffffffff"


def returns() -> list[dict[str, Any]]:
    """Open returns as GET /returns returns them."""
    return [
        {
            "id": RETURN_OPEN,
            "order_id": RETURN_ORDER,
            "shop": "Example Shop",
            "order_number": "ORDER-0001",
            "status": "label_created",
            "reason": None,
            "return_by": "2999-02-01",
            "drop_off": [
                {"carrier": "dhl", "kind": "parcel_locker", "description": None},
                {"carrier": None, "kind": "shop", "description": "Any Example store"},
            ],
            "cost": {"free": True, "amount": None, "currency": None},
            "label_url": "https://shop.example.test/label/1",
            "carrier": "dhl",
            "tracking_number": None,
            "parcel_id": None,
            "refund": None,
            "items": [{"title": "Example shoes", "order_item_position": 1}],
            "created_at": "2999-01-01T10:00:00.000Z",
            "updated_at": "2999-01-01T10:00:00.000Z",
        }
    ]
