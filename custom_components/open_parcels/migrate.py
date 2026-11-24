"""Config entry migrations: stored ids follow the server's names.

Version 2 (server 2.0, glossary pass): a "source" is a "connection", and parcel
directions are "receiving" and "sending". Entity unique ids and connection device
identifiers are rewritten in place, so entity ids, names and history stay.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN

# Version 1 entity keys -> version 2 keys (hub and connection sensors alike).
_V1_KEYS = {
    "incoming": "receiving",
    "outgoing": "sending",
    "outgoing_delivered": "sending_delivered",
    "incoming_parcels": "receiving_parcels",
    "outgoing_parcels": "sending_parcels",
    "outgoing_delivered_parcels": "sending_delivered_parcels",
    "source_state": "connection_state",
}
_V1_CONNECTION = "source_"
_V2_CONNECTION = "connection_"


def _v2_key(key: str) -> str:
    return _V1_KEYS.get(key, key)


def v2_unique_suffix(suffix: str) -> str:
    """A version 1 unique id suffix (after "<entry_id>_") in version 2 terms."""
    if not suffix.startswith(_V1_CONNECTION):
        return _v2_key(suffix)
    rest = suffix[len(_V1_CONNECTION) :]
    # "<connection id>_<key>": the key is the longest known ending.
    for key in sorted(_V1_KEYS, key=len, reverse=True):
        if rest.endswith(f"_{key}"):
            rest = f"{rest[: -len(key)]}{_V1_KEYS[key]}"
            break
    return f"{_V2_CONNECTION}{rest}"


async def _migrate_v1_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    prefix = f"{entry.entry_id}_"

    @callback
    def _entity(entity: er.RegistryEntry) -> dict[str, str] | None:
        if not entity.unique_id.startswith(prefix):
            return None
        new = prefix + v2_unique_suffix(entity.unique_id[len(prefix) :])
        return {"new_unique_id": new} if new != entity.unique_id else None

    await er.async_migrate_entries(hass, entry.entry_id, _entity)

    devices = dr.async_get(hass)
    old_prefix = f"{prefix}{_V1_CONNECTION}"
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        identifiers = {
            (domain, f"{prefix}{_V2_CONNECTION}{value[len(old_prefix) :]}")
            if domain == DOMAIN and value.startswith(old_prefix)
            else (domain, value)
            for domain, value in device.identifiers
        }
        if identifiers != device.identifiers:
            devices.async_update_device(device.id, new_identifiers=identifiers)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry from an older version to the current one."""
    if entry.version > 2:
        # Written by a newer release: refuse rather than guess.
        return False
    if entry.version == 1:
        await _migrate_v1_ids(hass, entry)
        hass.config_entries.async_update_entry(entry, version=2)
    return True
