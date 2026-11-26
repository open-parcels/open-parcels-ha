"""Services: track a code, archive a parcel, refresh, run a connection's action, cancel its job."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api import OpenParcelsApiError, OpenParcelsError, OpenParcelsNotFoundError
from .const import DOMAIN
from .device import connection_identifier
from .jobs import connection_actions
from .schema import vol

if TYPE_CHECKING:
    from . import OpenParcelsConfigEntry

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_ARCHIVE_PARCEL = "archive_parcel"
SERVICE_REFRESH = "refresh"
SERVICE_RUN_ACTION = "run_action"
SERVICE_CANCEL_JOB = "cancel_job"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TRACKING_NUMBER = "tracking_number"
ATTR_PARCEL_ID = "parcel_id"
ATTR_CARRIER = "carrier"
ATTR_LABEL = "label"
ATTR_DIRECTION = "direction"
ATTR_DEVICE_ID = "device_id"
ATTR_ACTION = "action"
ATTR_INPUT = "input"
ATTR_CONFIRM = "confirm"

_ENTRY = vol.Optional(ATTR_CONFIG_ENTRY_ID)
# The server's own bounds: tracking codes 4-64 characters, carrier ids 1-64.
_CODE = vol.All(cv.string, vol.Strip, vol.Length(4, 64))


def _uuid(value: str) -> str:
    """A parcel id: a UUID, as the server writes it (lower case)."""
    try:
        return str(uuid.UUID(value))
    except ValueError as err:
        raise vol.Invalid("parcel_id must be a UUID") from err


TRACK_SCHEMA = vol.Schema(
    {
        _ENTRY: vol.All(cv.string, vol.Strip, vol.Length(min=1)),
        vol.Required(ATTR_TRACKING_NUMBER): _CODE,
        vol.Optional(ATTR_CARRIER): vol.All(cv.string, vol.Strip, vol.Length(1, 64)),
        vol.Optional(ATTR_LABEL): vol.All(cv.string, vol.Length(max=120)),
        vol.Optional(ATTR_DIRECTION, default="receiving"): vol.In(
            ["receiving", "sending"]
        ),
    }
)
PARCEL_SCHEMA = vol.Schema(
    {
        _ENTRY: vol.All(cv.string, vol.Strip, vol.Length(min=1)),
        vol.Optional(ATTR_TRACKING_NUMBER): _CODE,
        vol.Optional(ATTR_PARCEL_ID): vol.All(cv.string, vol.Strip, _uuid),
    }
)

RUN_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_ACTION): vol.All(cv.string, vol.Match(r"^[a-z0-9_]{1,40}$")),
        vol.Optional(ATTR_INPUT, default={}): dict,
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
    }
)
CANCEL_JOB_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def _entry(hass: HomeAssistant, call: ServiceCall) -> OpenParcelsConfigEntry:
    """The entry the call names, or the only loaded one."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        entries = [entry for entry in entries if entry.entry_id == wanted]
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_loaded_entry"
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_required"
        )
    return entries[0]


def _find_parcel(
    entry: OpenParcelsConfigEntry, call: ServiceCall
) -> dict[str, Any] | None:
    """The parcel named by id or tracking code; None when neither is given."""
    parcel_id = call.data.get(ATTR_PARCEL_ID)
    code = call.data.get(ATTR_TRACKING_NUMBER)
    if not parcel_id and not code:
        return None
    data = entry.runtime_data.coordinator.data
    parcels = data.parcels.values() if data else []
    for parcel in parcels:
        if (parcel_id and parcel.get("id") == parcel_id) or (
            code
            and str(parcel.get("tracking_number", "")).casefold()
            == code.strip().casefold()
        ):
            return parcel
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="parcel_not_found",
        translation_placeholders={"parcel": str(parcel_id or code)},
    )


def _api_error(err: OpenParcelsError) -> HomeAssistantError:
    if isinstance(err, OpenParcelsApiError) and err.status == 429 and err.message:
        # A connection backing off: the server's message says how long to wait.
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="sync_backing_off",
            translation_placeholders={"message": err.message},
        )
    if isinstance(err, OpenParcelsApiError) and err.status in (400, 409):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="request_refused",
            translation_placeholders={"error": err.message or str(err)},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="request_failed",
        translation_placeholders={"error": str(err)},
    )


async def _track(call: ServiceCall) -> ServiceResponse:
    entry = _entry(call.hass, call)
    try:
        parcel = await entry.runtime_data.client.async_track_parcel(
            call.data[ATTR_TRACKING_NUMBER],
            carrier=call.data.get(ATTR_CARRIER),
            label=call.data.get(ATTR_LABEL),
            direction=call.data[ATTR_DIRECTION],
        )
    except OpenParcelsError as err:
        raise _api_error(err) from err
    await entry.runtime_data.coordinator.async_request_refresh()
    return {"parcel_id": parcel.get("id"), "carrier": parcel.get("carrier")}


async def _archive(call: ServiceCall) -> None:
    entry = _entry(call.hass, call)
    parcel = _find_parcel(entry, call)
    if parcel is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="parcel_required"
        )
    coordinator = entry.runtime_data.coordinator
    try:
        await entry.runtime_data.client.async_archive_parcel(str(parcel["id"]))
    except OpenParcelsNotFoundError as err:
        # Gone on the server (deleted, or hidden with its disabled connection).
        coordinator.async_forget_parcel(str(parcel["id"]))
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="parcel_not_found",
            translation_placeholders={
                "parcel": str(parcel.get("tracking_number") or "")
            },
        ) from err
    except OpenParcelsError as err:
        raise _api_error(err) from err
    await coordinator.async_request_refresh()


async def _refresh(call: ServiceCall) -> None:
    """Without a parcel: resync. With one: run the connections that follow it first."""
    entry = _entry(call.hass, call)
    parcel = _find_parcel(entry, call)
    if parcel is not None:
        connection_ids = {
            str(ref["connection_id"])
            for ref in parcel.get("connections") or []
            if isinstance(ref, dict) and ref.get("connection_id")
        }
        try:
            for connection_id in sorted(connection_ids):
                await entry.runtime_data.client.async_sync_connection(connection_id)
        except OpenParcelsError as err:
            raise _api_error(err) from err
    await entry.runtime_data.coordinator.async_request_refresh()


def _connection_of_device(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[OpenParcelsConfigEntry, str]:
    """The loaded entry and the connection whose device the call names."""
    device = dr.async_get(hass).async_get(call.data[ATTR_DEVICE_ID])
    for entry in hass.config_entries.async_entries(DOMAIN):
        if device is None or entry.state is not ConfigEntryState.LOADED:
            continue
        data = entry.runtime_data.coordinator.data
        for connection_id in data.connections if data else ():
            if (
                connection_identifier(entry.entry_id, connection_id)
                in device.identifiers
            ):
                return entry, connection_id
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="connection_device_required"
    )


async def _run_action(call: ServiceCall) -> ServiceResponse:
    """Start an action; a destructive one only with `confirm: true`."""
    entry, connection_id = _connection_of_device(call.hass, call)
    data = entry.runtime_data.coordinator.data
    action = next(
        (
            a
            for a in connection_actions(data, connection_id)
            if a.get("id") == call.data[ATTR_ACTION]
        ),
        None,
    )
    if action is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_such_action",
            translation_placeholders={"action": call.data[ATTR_ACTION]},
        )
    if action.get("destructive") is True and not call.data[ATTR_CONFIRM]:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="confirm_required"
        )
    try:
        answer = await entry.runtime_data.client.async_run_action(
            connection_id, str(action["id"]), dict(call.data[ATTR_INPUT])
        )
    except OpenParcelsError as err:
        raise _api_error(err) from err
    return {"url": answer.get("url")}


async def _cancel_job(call: ServiceCall) -> None:
    """Cancel the job the connection is busy with."""
    entry, connection_id = _connection_of_device(call.hass, call)
    data = entry.runtime_data.coordinator.data
    job = data.jobs.get(connection_id) if data else None
    if job is None or job.get("state") not in ("queued", "running", "paused"):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_active_job"
        )
    try:
        await entry.runtime_data.client.async_cancel_job(str(job["id"]))
    except OpenParcelsError as err:
        raise _api_error(err) from err


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRACK_PARCEL,
        _track,
        schema=TRACK_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ARCHIVE_PARCEL, _archive, schema=PARCEL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _refresh, schema=PARCEL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_ACTION,
        _run_action,
        schema=RUN_ACTION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_JOB, _cancel_job, schema=CANCEL_JOB_SCHEMA
    )
