"""Calendar of expected and delivered parcels, read from the server's calendar."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OpenParcelsConfigEntry
from .api import OpenParcelsError
from .const import DOMAIN
from .coordinator import OpenParcelsCoordinator
from .entity import OpenParcelsEntity, device_for_connection
from .parcels import parse_iso

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenParcelsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the deliveries calendar on the hub."""
    async_add_entities([DeliveriesCalendar(entry.runtime_data.coordinator)])


def _local_days(entry: Mapping[str, Any]) -> tuple[date, date] | None:
    """The all-day entry's `start_date` and exclusive `end_date`, when the server sends them."""
    try:
        start = date.fromisoformat(str(entry["start_date"]))
        end = date.fromisoformat(str(entry["end_date"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (start, end) if end > start else None


def _summary_and_lines(entry: Mapping[str, Any]) -> tuple[str, list[str | None]]:
    """The event's title and description lines; returns list drop-off and cost."""
    title = str(entry.get("title") or entry.get("tracking_number") or "Parcel")
    status = f"Status: {entry['status']}" if entry.get("status") else None
    if entry.get("kind") == "return_by":
        # A return's `tracking_number` is its order number; `details` its drop-off and cost.
        details = [str(line) for line in entry.get("details") or [] if line]
        order = (
            f"Order: {entry['tracking_number']}"
            if entry.get("tracking_number")
            else None
        )
        label = f"Label: {entry['url']}" if entry.get("url") else None
        return f"Return by: {title}", [order, *details, status, label]
    summary = f"Delivered: {title}" if entry.get("kind") == "delivered" else title
    carrier_code = " ".join(
        str(entry[k]) for k in ("carrier", "tracking_number") if entry.get(k)
    )
    return summary, [carrier_code, status, entry.get("url")]


def to_calendar_event(entry: Mapping[str, Any]) -> CalendarEvent | None:
    """Turn one server calendar entry into a CalendarEvent."""
    start = parse_iso(entry.get("start"))
    end = parse_iso(entry.get("end"))
    if start is None or end is None:
        return None
    summary, lines = _summary_and_lines(entry)
    description = "\n".join(line for line in lines if line) or None
    event_start: date | datetime = start
    event_end: date | datetime = end
    days = _local_days(entry)
    if entry.get("all_day") and days is not None:
        # The server's own local days, so Home Assistant's time zone cannot shift them.
        event_start, event_end = days
    elif entry.get("all_day"):
        # No usable local days: the instants' whole-day window ends at midnight
        # (exclusive) or at the last second of the day (inclusive); both become an
        # exclusive end date.
        event_start = dt_util.as_local(start).date()
        event_end = (dt_util.as_local(end) - timedelta(seconds=1)).date() + timedelta(
            days=1
        )
        if event_end <= event_start:
            event_end = event_start + timedelta(days=1)
    elif end <= start:
        event_end = start + timedelta(hours=1)
    return CalendarEvent(
        start=event_start,
        end=event_end,
        summary=summary,
        description=description,
        location=entry.get("pickup_point"),
        uid=entry.get("uid"),
    )


def _events(entries: list[dict[str, Any]]) -> list[CalendarEvent]:
    return [event for entry in entries if (event := to_calendar_event(entry))]


class DeliveriesCalendar(OpenParcelsEntity, CalendarEntity):
    """The same entries as the server's calendar and ICS feed."""

    _attr_translation_key = "deliveries"
    # Tracking codes, links and pickup points stay out of history (HA-024).
    _unrecorded_attributes = frozenset({"description", "location"})

    def __init__(self, coordinator: OpenParcelsCoordinator) -> None:
        """Belongs to the hub."""
        super().__init__(coordinator, "deliveries")
        self._attr_device_info = device_for_connection(coordinator.config_entry, None)

    @property
    def event(self) -> CalendarEvent | None:
        """The current or next expected delivery (delivered markers are skipped)."""
        now = dt_util.now()
        expected = [
            entry
            for entry in self.coordinator.calendar_entries
            if entry.get("kind") != "delivered"
        ]
        upcoming = [
            event for event in _events(expected) if event.end_datetime_local > now
        ]
        return min(upcoming, key=lambda e: e.start_datetime_local, default=None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Ask the server for the range."""
        try:
            entries = await self.coordinator.client.async_get_calendar(
                start_date, end_date
            )
        except OpenParcelsError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="calendar_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return _events(entries)
