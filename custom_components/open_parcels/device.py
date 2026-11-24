"""Devices: one hub for the server, one device per Open Parcels connection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN

HUB_NAME = "Open Parcels"


def hub_identifier(entry_id: str) -> tuple[str, str]:
    """Device identifier of the hub."""
    return (DOMAIN, entry_id)


def connection_identifier(entry_id: str, connection_id: str) -> tuple[str, str]:
    """Device identifier of a connection."""
    return (DOMAIN, f"{entry_id}_connection_{connection_id}")


def find_device(
    hass: HomeAssistant, entry_id: str, identifier: tuple[str, str]
) -> dr.DeviceEntry | None:
    """This entry's device with an identifier.

    Searches the entry's own devices, which works on every supported Home
    Assistant release (the identifier lookup changed in newer ones).
    """
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id):
        if identifier in device.identifiers:
            return device
    return None


def server_url(entry: ConfigEntry) -> str:
    """The server URL stored in the entry."""
    return str(entry.data.get("url", "")).rstrip("/")


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    """DeviceInfo of the hub every summary entity belongs to."""
    return DeviceInfo(
        identifiers={hub_identifier(entry.entry_id)},
        name=HUB_NAME,
        manufacturer=HUB_NAME,
        model="Server",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=server_url(entry) or None,
    )


def connection_device_name(
    connection: Mapping[str, Any], carriers: Mapping[str, str]
) -> str:
    """Name a connection's device "<carrier> <label>", unless the label already says it.

    With the connection labelled like the old integration's entry title (for
    example "Germany (7c6c9a)"), the device name, and therefore every entity
    id derived from it, matches what the per-carrier integration produced.
    """
    provider = str(connection.get("provider") or "")
    carrier = carriers.get(provider, provider)
    label = str(connection.get("label") or "").strip()
    if not label:
        return carrier
    if not carrier or label.casefold().startswith(carrier.casefold()):
        return label
    return f"{carrier} {label}"


def connection_device_info(
    entry: ConfigEntry, connection: Mapping[str, Any], carriers: Mapping[str, str]
) -> DeviceInfo:
    """DeviceInfo of one connection.

    The link to the hub is set by device id when the device is registered
    (``via_device_id``), which every supported Home Assistant release accepts.
    """
    provider = str(connection.get("provider") or "")
    return DeviceInfo(
        identifiers={connection_identifier(entry.entry_id, str(connection["id"]))},
        name=connection_device_name(connection, carriers),
        manufacturer=carriers.get(provider, provider) or None,
        model=str(connection.get("kind") or "") or None,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=server_url(entry) or None,
    )
