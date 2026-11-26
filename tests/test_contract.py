"""The fixtures carry every key the server contract requires (HA-030).

`contract.json` holds the required keys from the server's `docs/openapi.json`, pinned to
one server version, so a drifting fixture fails here instead of hiding a bug.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from . import payloads

REQUIRED: dict[str, list[str]] = json.loads(
    (Path(__file__).parent / "contract.json").read_text(encoding="utf-8")
)


def _missing(kind: str, objects: list[dict[str, Any]]) -> list[str]:
    assert objects, f"no {kind} fixtures"
    return [
        f"{kind} {obj.get('id') or obj.get('uid') or index}: {key}"
        for index, obj in enumerate(objects)
        for key in REQUIRED[kind]
        if key not in obj
    ]


@pytest.mark.parametrize(
    ("kind", "objects"),
    [
        ("parcel", payloads.parcels()),
        (
            "parcel_connection_ref",
            [ref for parcel in payloads.parcels() for ref in parcel["connections"]],
        ),
        ("connection", payloads.CONNECTIONS),
        ("carrier", payloads.CARRIERS),
        ("action", [*payloads.ACTIONS, payloads.REREAD_ACTION]),
        ("job", payloads.jobs()),
        ("calendar_entry", payloads.calendar_entries()),
        ("return", payloads.returns()),
    ],
)
def test_fixtures_have_the_required_keys(
    kind: str, objects: list[dict[str, Any]]
) -> None:
    assert not _missing(kind, objects)
