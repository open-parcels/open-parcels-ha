"""Device triggers for the parcel and connection events.

A connection device's triggers fire for that connection's parcels; the hub's fire for
every parcel of the server.
"""

from __future__ import annotations

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, RETURN_EVENT_SUFFIXES, SERVER_EVENT_SUFFIXES
from .schema import vol

# Device-trigger type -> bus event; the type is the event name without the prefix.
TRIGGER_EVENTS = {
    suffix: f"{DOMAIN}_{suffix}"
    for suffix in (*SERVER_EVENT_SUFFIXES.values(), *RETURN_EVENT_SUFFIXES.values())
}
TRIGGER_TYPES = set(TRIGGER_EVENTS)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _is_hub(hass: HomeAssistant, device_id: str) -> bool:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return False
    # Hub identifiers are the bare entry id; connection ones carry "_connection_".
    return any(
        domain == DOMAIN and "_connection_" not in identifier
        for domain, identifier in device.identifiers
    )


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the triggers of an Open Parcels device."""
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in sorted(TRIGGER_TYPES)
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger as an event trigger filtered on the device."""
    device_id = config[CONF_DEVICE_ID]
    key = "hub_device_id" if _is_hub(hass, device_id) else CONF_DEVICE_ID
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: TRIGGER_EVENTS[config[CONF_TYPE]],
            CONF_EVENT_DATA: {key: device_id},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
