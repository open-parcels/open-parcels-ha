"""Connection actions and jobs: buttons, the job sensor, services and bus events."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.api import OpenParcelsApiError
from custom_components.open_parcels.const import DOMAIN
from custom_components.open_parcels.device import find_device

from . import payloads
from .conftest import FakeStream, events

JOB_SENSOR = "sensor.dhl_germany_abc123_job"
IMPORT_BUTTON = "button.dhl_germany_abc123_import_history"


def _device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = find_device(
        hass,
        entry.entry_id,
        (DOMAIN, f"{entry.entry_id}_connection_{payloads.CONNECTION_DHL}"),
    )
    assert device is not None
    return device.id


def _event(**job: Any) -> str:
    return json.dumps(
        {
            "job": payloads.job(**job),
            "connection": {
                "id": payloads.CONNECTION_DHL,
                "provider": "dhl",
                "label": "Germany (abc123)",
                "kind": "account",
            },
        }
    )


async def test_job_sensor_follows_the_live_events(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The newest job is loaded with the connections; job events move it on."""
    state = hass.states.get(JOB_SENSOR)
    assert state is not None
    assert state.state == "running"
    assert state.attributes["done"] == 2
    assert state.attributes["total"] == 5
    assert state.attributes["phase"] == "2029: page 3"

    finished = events(hass, f"{DOMAIN}_job_finished")
    message = _event(
        state="done",
        summary="412 orders",
        progress={"done": 5, "total": 5, "phase": None},
        updated_at="2030-01-02T09:00:00.000Z",
    )
    stream.push("job.finished", message, "70")
    # A replay of the same message changes nothing and fires nothing again.
    stream.push("job.finished", message, "70")
    await hass.async_block_till_done()
    state = hass.states.get(JOB_SENSOR)
    assert state is not None
    assert state.state == "done"
    assert state.attributes["summary"] == "412 orders"
    assert len(finished) == 1
    assert finished[0].data["action"] == "import_history"
    assert finished[0].data["state"] == "done"
    assert finished[0].data["device_id"] == _device_id(hass, loaded)

    # An older event of the same job does not move the sensor back.
    stream.push("job.progress", _event(updated_at="2030-01-02T08:40:00.000Z"), "71")
    await hass.async_block_till_done()
    assert hass.states.get(JOB_SENSOR).state == "done"


async def test_a_paused_job_goes_on_the_bus(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """job.paused fires open_parcels_job_paused with the reason and when it continues."""
    paused = events(hass, f"{DOMAIN}_job_paused")
    stream.push(
        "job.paused",
        _event(
            state="paused",
            reason="rate_limited",
            resume_at="2030-01-02T09:15:00.000Z",
            updated_at="2030-01-02T09:00:00.000Z",
        ),
        "72",
    )
    await hass.async_block_till_done()
    assert [event.data["reason"] for event in paused] == ["rate_limited"]
    assert paused[0].data["resume_at"] == "2030-01-02T09:15:00.000Z"


async def test_only_actions_meant_for_home_assistant_become_buttons(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """A task marked for HA without input becomes a button; links, input and destructive don't."""
    registry = er.async_get(hass)
    actions = [
        entity.unique_id.rsplit("_action_", 1)[1]
        for entity in er.async_entries_for_config_entry(registry, loaded.entry_id)
        if "_action_" in entity.unique_id
    ]
    assert actions == ["import_history"]
    await hass.services.async_call(
        "button", "press", {"entity_id": IMPORT_BUTTON}, blocking=True
    )
    client.async_run_action.assert_awaited_once_with(
        payloads.CONNECTION_DHL, "import_history", {}
    )
    client.async_run_action.side_effect = OpenParcelsApiError(
        409, "job_running", "This connection has a job already."
    )
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            "button", "press", {"entity_id": IMPORT_BUTTON}, blocking=True
        )
    assert raised.value.translation_key == "action_refused"


async def test_run_action_and_cancel_job_services(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """The services take the connection's device; a destructive action needs confirm."""
    device_id = _device_id(hass, loaded)
    client.async_run_action.return_value = {"url": None}
    await hass.services.async_call(
        DOMAIN,
        "run_action",
        {"device_id": device_id, "action": "by_year", "input": {"year": 2029}},
        blocking=True,
    )
    client.async_run_action.assert_awaited_with(
        payloads.CONNECTION_DHL, "by_year", {"year": 2029}
    )
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "run_action",
            {"device_id": device_id, "action": "wipe"},
            blocking=True,
        )
    assert raised.value.translation_key == "confirm_required"
    await hass.services.async_call(
        DOMAIN,
        "run_action",
        {"device_id": device_id, "action": "wipe", "confirm": True},
        blocking=True,
    )
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "run_action",
            {"device_id": device_id, "action": "nope"},
            blocking=True,
        )
    assert raised.value.translation_key == "no_such_action"

    await hass.services.async_call(
        DOMAIN, "cancel_job", {"device_id": device_id}, blocking=True
    )
    client.async_cancel_job.assert_awaited_once_with(payloads.JOB_DHL)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, "cancel_job", {"device_id": "not-a-device"}, blocking=True
        )
    assert raised.value.translation_key == "connection_device_required"


async def test_inbox_actions_become_buttons_on_inboxes(
    hass: HomeAssistant, loaded: MockConfigEntry, client: MagicMock
) -> None:
    """The inbox's actions come from GET /inbox-form, not /carriers; an inbox gets its button."""
    inbox = {
        **payloads.CONNECTIONS[0],
        "id": "33333333-3333-4333-8333-333333333333",
        "provider": "imap",
        "kind": "mail",
        "label": "Mail (user@example.invalid)",
        "config": {},
    }
    client.async_get_connections.side_effect = lambda: [
        *(dict(c) for c in payloads.CONNECTIONS),
        dict(inbox),
    ]
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.open_parcels_refresh"}, blocking=True
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    button = next(
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, loaded.entry_id)
        if entity.unique_id.endswith(f"{inbox['id']}_action_reread_emails")
    )
    state = hass.states.get(button)
    assert state is not None
    assert state.attributes["friendly_name"].endswith("Read old emails again")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    client.async_run_action.assert_awaited_once_with(inbox["id"], "reread_emails", {})
