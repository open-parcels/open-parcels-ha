"""Words in Home Assistant's language for the calendar, the returns sensor and its events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

# Translation groups under `selector` in strings.json.
_TEXT_GROUPS = ("calendar_text", "drop_off_kind", "parcel_status", "return_status")


async def async_texts(hass: HomeAssistant) -> dict[str, str]:
    """The words in Home Assistant's language, keyed `<group>.<option>`."""
    loaded = await async_get_translations(
        hass, hass.config.language, "selector", {DOMAIN}
    )
    prefix = f"component.{DOMAIN}.selector."
    texts: dict[str, str] = {}
    for key, value in loaded.items():
        if not key.startswith(prefix):
            continue
        group, _, option = key[len(prefix) :].partition(".options.")
        if group in _TEXT_GROUPS and option:
            texts[f"{group}.{option}"] = value
    return texts


def text(texts: Mapping[str, str], key: str, **values: str) -> str:
    """One translated text with its values filled in; the key itself when it is missing."""
    template = texts.get(key)
    if template is None:
        return key
    for name, value in values.items():
        template = template.replace(f"{{{name}}}", value)
    return template


def drop_off_text(texts: Mapping[str, str], option: Mapping[str, Any]) -> str:
    """One drop-off option: the shop's description, else its kind and carrier."""
    if option.get("description"):
        return str(option["description"])
    kind = str(option.get("kind") or "any")
    label = texts.get(f"drop_off_kind.{kind}") or kind.replace("_", " ").capitalize()
    carrier = option.get("carrier")
    return f"{label} ({carrier})" if carrier else label


def cost_text(texts: Mapping[str, str], cost: Any) -> str | None:
    """The cost in words, or None when it is not known."""
    if not isinstance(cost, Mapping):
        return None
    if cost.get("free"):
        return text(texts, "calendar_text.free")
    amount = cost.get("amount")
    if not isinstance(amount, int | float):
        return text(texts, "calendar_text.costs_money")
    currency = cost.get("currency")
    shown = f"{amount:.2f}" + (f" {currency}" if currency else "")
    return text(texts, "calendar_text.costs", amount=shown)
