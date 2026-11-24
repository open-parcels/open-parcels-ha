"""Services: track a code, archive a parcel, refresh."""

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
from homeassistant.helpers import config_validation as cv

from .api import OpenParcelsApiError, OpenParcelsError, OpenParcelsNotFoundError
from .const import DOMAIN
from .schema import vol

if TYPE_CHECKING:
    from . import OpenParcelsConfigEntry

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_ARCHIVE_PARCEL = "archive_parcel"
SERVICE_REFRESH = "refresh"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TRACKING_CODE = "tracking_code"
ATTR_PARCEL_ID = "parcel_id"
ATTR_CARRIER = "carrier"
ATTR_LABEL = "label"
ATTR_DIRECTION = "direction"

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
        vol.Required(ATTR_TRACKING_CODE): _CODE,
        vol.Optional(ATTR_CARRIER): vol.All(cv.string, vol.Strip, vol.Length(1, 64)),
        vol.Optional(ATTR_LABEL): vol.All(cv.string, vol.Length(max=120)),
        vol.Optional(ATTR_DIRECTION, default="incoming"): vol.In(
            ["incoming", "outgoing"]
        ),
    }
)
PARCEL_SCHEMA = vol.Schema(
    {
        _ENTRY: vol.All(cv.string, vol.Strip, vol.Length(min=1)),
        vol.Optional(ATTR_TRACKING_CODE): _CODE,
        vol.Optional(ATTR_PARCEL_ID): vol.All(cv.string, vol.Strip, _uuid),
    }
)


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
    code = call.data.get(ATTR_TRACKING_CODE)
    if not parcel_id and not code:
        return None
    data = entry.runtime_data.coordinator.data
    parcels = data.parcels.values() if data else []
    for parcel in parcels:
        if (parcel_id and parcel.get("id") == parcel_id) or (
            code
            and str(parcel.get("barcode", "")).casefold() == code.strip().casefold()
        ):
            return parcel
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="parcel_not_found",
        translation_placeholders={"parcel": str(parcel_id or code)},
    )


def _api_error(err: OpenParcelsError) -> HomeAssistantError:
    if isinstance(err, OpenParcelsApiError) and err.status == 429 and err.message:
        # A source backing off: the server's message says how long to wait.
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
            call.data[ATTR_TRACKING_CODE],
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
        # Gone on the server (deleted, or hidden with its disabled source).
        coordinator.async_forget_parcel(str(parcel["id"]))
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="parcel_not_found",
            translation_placeholders={"parcel": str(parcel.get("barcode") or "")},
        ) from err
    except OpenParcelsError as err:
        raise _api_error(err) from err
    await coordinator.async_request_refresh()


async def _refresh(call: ServiceCall) -> None:
    """Without a parcel: resync. With one: run the sources that follow it first."""
    entry = _entry(call.hass, call)
    parcel = _find_parcel(entry, call)
    if parcel is not None:
        source_ids = {
            str(ref["source_id"])
            for ref in parcel.get("sources") or []
            if isinstance(ref, dict) and ref.get("source_id")
        }
        try:
            for source_id in sorted(source_ids):
                await entry.runtime_data.client.async_sync_source(source_id)
        except OpenParcelsError as err:
            raise _api_error(err) from err
    await entry.runtime_data.coordinator.async_request_refresh()


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
