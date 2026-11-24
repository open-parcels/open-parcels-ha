"""Returns in words: the compact shape the sensor and the bus events carry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The server's own words for a drop-off kind (domain/return.ts).
_KIND_LABELS = {
    "shop": "Shop",
    "parcel_locker": "Parcel locker",
    "post_office": "Post office",
    "pickup": "Pickup",
    "any": "Any drop-off point",
}


def drop_off_text(option: Mapping[str, Any]) -> str:
    """One drop-off option: the shop's description, else its kind and carrier."""
    if option.get("description"):
        return str(option["description"])
    kind = str(option.get("kind") or "any")
    label = _KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    carrier = option.get("carrier")
    return f"{label} ({carrier})" if carrier else label


def cost_text(cost: Any) -> str | None:
    """The cost in words, or None when it is not known."""
    if not isinstance(cost, Mapping):
        return None
    if cost.get("free"):
        return "Free return"
    amount = cost.get("amount")
    if not isinstance(amount, int | float):
        return "Return costs money"
    currency = cost.get("currency")
    return f"Return costs {amount:.2f}" + (f" {currency}" if currency else "")


def return_summary(ret: Mapping[str, Any]) -> dict[str, Any]:
    """Order, shop, status, return-by day, drop-off options, cost and label URL."""
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
            drop_off_text(option)
            for option in ret.get("drop_off") or []
            if isinstance(option, Mapping)
        ],
        "free": cost.get("free") if cost else None,
        "cost": cost_text(cost),
        "label_url": ret.get("label_url"),
    }
