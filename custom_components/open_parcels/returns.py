"""Returns in words: the compact shape the sensor and the bus events carry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .texts import cost_text, drop_off_text


def return_summary(ret: Mapping[str, Any], texts: Mapping[str, str]) -> dict[str, Any]:
    """Order, shop, status, return-by day, drop-off options, cost and label URL.

    Drop-off options and cost are worded with `texts` (Home Assistant's language).
    """
    cost = ret.get("cost") if isinstance(ret.get("cost"), Mapping) else None
    items = [
        str(item.get("title"))
        for item in ret.get("items") or []
        if isinstance(item, Mapping) and item.get("title")
    ]
    return {
        "order_number": ret.get("order_number"),
        "shop": ret.get("shop"),
        "items": items,
        "status": ret.get("status"),
        "return_by": ret.get("return_by"),
        "drop_off": [
            drop_off_text(texts, option)
            for option in ret.get("drop_off") or []
            if isinstance(option, Mapping)
        ],
        "free": cost.get("free") if cost else None,
        "cost": cost_text(texts, cost),
        "label_url": ret.get("label_url"),
    }
