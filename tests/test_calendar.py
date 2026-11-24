"""Calendar."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.api import OpenParcelsConnectionError
from custom_components.open_parcels.calendar import to_calendar_event


async def test_calendar(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """The state is the next expected delivery; ranges come from the server."""
    await hass.config.async_set_time_zone("UTC")
    state = hass.states.get("calendar.open_parcels_deliveries")
    assert state is not None
    assert state.attributes["message"] == "Example Shop"

    entity = hass.data["calendar"].get_entity("calendar.open_parcels_deliveries")
    start = dt_util.utcnow()
    events = await entity.async_get_events(hass, start, start + timedelta(days=7))
    assert [e.summary for e in events] == [
        "Example Shop",
        "Example Kiosk parcel",
        "Delivered: Delivered thing",
    ]
    # Whole days in the server's zone become dates in Home Assistant's zone.
    assert isinstance(events[1].start, date)
    assert events[1].end - events[1].start == timedelta(days=1)
    assert events[1].location == "Example Kiosk"
    # A zero-length delivered marker gets an hour.
    assert events[2].end - events[2].start == timedelta(hours=1)

    client.async_get_calendar.side_effect = OpenParcelsConnectionError("down")
    with pytest.raises(HomeAssistantError):
        await entity.async_get_events(hass, start, start + timedelta(days=7))


async def test_calendar_entry_edge_cases(hass: HomeAssistant) -> None:
    """Unparseable entries are skipped; inclusive day ends become exclusive dates."""
    await hass.config.async_set_time_zone("UTC")
    assert to_calendar_event({"start": "nope", "end": None}) is None
    event = to_calendar_event(
        {
            "start": "2999-01-03T00:00:00",
            "end": "2999-01-03T23:59:59",
            "all_day": True,
            "title": "",
            "tracking_number": "",
        }
    )
    assert event is not None
    assert event.summary == "Parcel"
    assert event.description is None
    assert event.end - event.start == timedelta(days=1)
    same = to_calendar_event(
        {"start": "2999-01-03T00:00:00", "end": "2999-01-03T00:00:00", "all_day": True}
    )
    assert same is not None and same.end > same.start


async def test_all_day_uses_the_server_local_days(hass: HomeAssistant) -> None:
    """A date-only delivery lands on the server's day, whatever HA's zone says."""
    await hass.config.async_set_time_zone("UTC")
    event = to_calendar_event(
        {
            # Midnight in the server's zone: the day before in UTC.
            "start": "2999-01-02T23:00:00Z",
            "end": "2999-01-03T23:00:00Z",
            "all_day": True,
            "start_date": "2999-01-03",
            "end_date": "2999-01-04",
            "kind": "delivered",
            "title": "Lamp",
        }
    )
    assert event is not None
    assert event.start == date(2999, 1, 3)
    assert event.end == date(2999, 1, 4)
    broken = to_calendar_event(
        {
            "start": "2999-01-03T00:00:00Z",
            "end": "2999-01-04T00:00:00Z",
            "all_day": True,
            "start_date": "not a date",
            "end_date": None,
        }
    )
    assert broken is not None and broken.start == date(2999, 1, 3)
