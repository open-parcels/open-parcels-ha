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


def _action(action_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "task",
        "label": action_id.replace("_", " ").capitalize(),
        "description": None,
        "connection_kinds": ["account"],
        "when_state": ["ok", "idle"],
        "weight": "light",
        "confirm": None,
        "destructive": False,
        "expose_to_ha": True,
        "cooldown_minutes": 0,
        "input_schema": None,
        "input_required": False,
        **overrides,
    }


# DHL's made-up actions: only import_history is a button (the others are a link, need
# input, are destructive, or are not meant for Home Assistant).
ACTIONS: list[dict[str, Any]] = [
    _action("import_history", label="Import history", weight="heavy"),
    _action("open_account", kind="link", expose_to_ha=False),
    _action("by_year", input_required=True, input_schema={"type": "object"}),
    _action("wipe", destructive=True),
    _action("web_only", expose_to_ha=False),
]

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
        "actions": actions,
    }
    for carrier_id, name, actions in (
        ("dhl", "DHL", [*ACTIONS]),
        ("gls", "GLS", []),
    )
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
        "state_reason": None,
        "carrier_message": None,
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
        "state_reason": "sign_in_rejected",
        "carrier_message": "Sign in again",
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
            "drop_off": [],
            "cost": None,
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
            "drop_off": [],
            "cost": None,
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
            "drop_off": [],
            "cost": None,
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


ORDER_EX = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"


def orders() -> list[dict[str, Any]]:
    """GET /orders (open): one made-up shop order with its stated total."""
    return copy.deepcopy(
        [
            {
                "id": ORDER_EX,
                "shop": "Example shop",
                "order_number": "EX-1",
                "label": None,
                "display_name": "Example shop EX-1",
                "placed_at": "2030-01-01T09:00:00.000Z",
                "status": "partly_shipped",
                "digital": False,
                "archived": False,
                "archived_at": None,
                "archive_mode": "automatic",
                "parcel_count": 1,
                "delivered_count": 0,
                "total": {"amount": 15.0, "currency": "EUR", "partial": False},
                "items": [],
                "return_until": None,
                "returns": [],
                "parcels": [],
                "created_at": "2030-01-01T09:00:00.000Z",
                "updated_at": "2030-01-01T09:00:00.000Z",
            }
        ]
    )


_CHECKS = ("database", "connections", "inboxes", "scheduler", "event_stream", "backups")


def status(overall: str = "ok") -> dict[str, Any]:
    """GET /status: every part fine unless `overall` says otherwise."""
    return {
        "status": overall,
        "checks": dict.fromkeys(_CHECKS, "ok") | {"backups": overall},
        "checked_at": "2030-01-02T12:00:00.000Z",
        "version": "2.0.0",
        "started_at": "2030-01-01T00:00:00.000Z",
        "database": {"detail": None},
        "connections": [],
        "inboxes": [],
        "scheduler": {
            "running": True,
            "in_flight": 0,
            "last_cycle_at": None,
            "last_error": None,
            "next_run_at": None,
        },
        "event_stream": {"clients": 1, "last_seq": 1, "stored": 1, "live_sessions": 0},
        "backups": {
            "enabled": True,
            "last_ok": True,
            "last_at": "2030-01-02T03:00:00.000Z",
            "last_error": None,
            "next_at": "2030-01-03T03:00:00.000Z",
        },
    }


def backups() -> dict[str, Any]:
    """GET /data/backups: one good scheduled backup."""
    return {
        "interval_hours": 24,
        "keep": 7,
        "directory": "/data/backups",
        "last": {
            "ok": True,
            "reason": "scheduled",
            "at": "2030-01-02T03:00:00.000Z",
            "file": "open-parcels-20300102-030000.db",
            "bytes": 4096,
            "error": None,
            "pruned": 0,
        },
        "next_at": "2030-01-03T03:00:00.000Z",
        "failing": False,
        "files": [],
    }


JOB_DHL = "0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b"


def job(**overrides: Any) -> dict[str, Any]:
    """A made-up import on the DHL connection, running."""
    return {
        "id": JOB_DHL,
        "connection_id": CONNECTION_DHL,
        "provider": "dhl",
        "action": "import_history",
        "state": "running",
        "reason": None,
        "progress": {"done": 2, "total": 5, "phase": "2029: page 3"},
        "summary": None,
        "created_at": "2030-01-02T08:00:00.000Z",
        "started_at": "2030-01-02T08:00:01.000Z",
        "updated_at": "2030-01-02T08:30:00.000Z",
        "finished_at": None,
        "resume_at": None,
        **overrides,
    }


def jobs() -> list[dict[str, Any]]:
    """GET /jobs: newest first."""
    return [job()]
