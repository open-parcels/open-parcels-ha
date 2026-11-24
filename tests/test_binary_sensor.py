"""Binary sensors."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_binary_sensors(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    """The stream is connected; the GLS connection needs a sign-in."""
    stream_state = hass.states.get("binary_sensor.open_parcels_event_stream")
    assert stream_state.state == "on"
    assert stream_state.attributes["events_received"] == 0
    assert hass.states.get("binary_sensor.gls_99999_problem").state == "on"
    assert hass.states.get("binary_sensor.dhl_germany_abc123_problem").state == "off"
