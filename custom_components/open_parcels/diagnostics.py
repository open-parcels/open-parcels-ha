"""Diagnostics with everything personal redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant

from . import OpenParcelsConfigEntry
from .const import CONF_API_TOKEN

# Diagnostics end up in public issues: redact anything that identifies a
# person, an address, a parcel or a secret.
TO_REDACT = {
    CONF_API_TOKEN,
    CONF_URL,
    "tracking_number",
    # The label, the item titles or the tracking code, depending on a server setting.
    "display_name",
    "shop",
    "sender",
    "receiver",
    "url",
    "image_url",
    "label",
    "title",
    "pickup_point",
    "address",
    "name",
    "order_number",
    "raw_status",
    "location",
    "latitude",
    "longitude",
    "message",
    "config",
    "detail",
    "carrier_message",
    "external_ref",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenParcelsConfigEntry
) -> dict[str, Any]:
    """Return the entry, the connections and the parcels, redacted."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    parcels = list(data.parcels.values()) if data else []
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "stream": {
            "connected": coordinator.stream_connected,
            "events_received": coordinator.events_received,
            "has_last_event_id": coordinator.last_event_id is not None,
        },
        "last_success_time": (
            coordinator.last_success_time.isoformat()
            if coordinator.last_success_time
            else None
        ),
        "carriers": dict(data.carriers) if data else {},
        "connections": async_redact_data(
            list(data.connections.values()) if data else [], TO_REDACT
        ),
        "parcel_count": len(parcels),
        "live_positions": len(coordinator.positions),
        "calendar_entries": len(coordinator.calendar_entries),
        "parcels": async_redact_data(parcels, TO_REDACT),
    }
