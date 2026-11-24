"""The event stream loop: connect, reconnect with Last-Event-ID, backoff, reauth."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import json
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels import stream as stream_module
from custom_components.open_parcels.api import (
    OpenParcelsAuthError,
    OpenParcelsConnectionError,
    OpenParcelsScopeError,
)
from custom_components.open_parcels.const import (
    STREAM_BACKOFF_INITIAL_SECONDS,
    STREAM_BACKOFF_MAX_SECONDS,
)

from .conftest import FakeStream


@pytest.fixture
def delays() -> Generator[list[float]]:
    """Record the reconnect delays and skip the actual waiting."""
    recorded: list[float] = []

    def _record(delay: float) -> float:
        recorded.append(delay)
        return 0

    with patch.object(stream_module, "jittered", _record):
        yield recorded


async def _settle(hass: HomeAssistant) -> None:
    """Let the background stream task run through several hops."""
    for _ in range(10):
        await hass.async_block_till_done()
        await asyncio.sleep(0)


def test_backoff_doubles_and_caps() -> None:
    """Backoff doubles to the cap; jitter adds at most a fifth."""
    assert stream_module.next_backoff(1) == 2
    assert stream_module.next_backoff(STREAM_BACKOFF_MAX_SECONDS) == (
        STREAM_BACKOFF_MAX_SECONDS
    )
    assert 10 <= stream_module.jittered(10) <= 12


async def test_first_connect_has_no_last_event_id(
    hass: HomeAssistant, loaded: MockConfigEntry, stream: FakeStream
) -> None:
    """The first connection never replays the past."""
    await _settle(hass)
    assert stream.calls == [None]
    assert hass.states.get("binary_sensor.open_parcels_event_stream").state == "on"


async def test_reconnect_sends_last_event_id(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
) -> None:
    """After a drop, the stream reconnects with the last id it saw."""
    stream.push("vocab.unknown", json.dumps({}), "7")
    stream.queue.put_nowait(OpenParcelsConnectionError("reset"))
    await _settle(hass)
    assert stream.calls == [None, "7"]
    assert delays == [STREAM_BACKOFF_INITIAL_SECONDS]


async def test_reconnect_without_id_resyncs(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    client: MagicMock,
    delays: list[float],
) -> None:
    """With no id seen yet, a reconnect is followed by a full resync."""
    before = client.async_get_parcels.await_count
    stream.queue.put_nowait(None)  # the server closes the stream
    await _settle(hass)
    assert stream.calls == [None, None]
    assert client.async_get_parcels.await_count == before + 1


async def test_unexpected_error_reconnects_instead_of_ending_the_stream(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
) -> None:
    """An error of an unexpected kind (aiohttp's "Chunk too big") is logged and reconnected."""
    stream.push("vocab.unknown", json.dumps({}), "8")
    stream.queue.put_nowait(ValueError("Chunk too big"))
    await _settle(hass)
    assert stream.calls == [None, "8"]
    assert hass.states.get("binary_sensor.open_parcels_event_stream").state == "on"


async def test_a_failing_message_is_skipped_and_the_stream_goes_on(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
) -> None:
    """A handler error on one message never ends the stream; later messages still apply."""
    coordinator = loaded.runtime_data.coordinator
    handled: list[str] = []
    original = coordinator.async_handle_message

    def handle(message):
        if message.event == "boom":
            raise RuntimeError("handler bug")
        handled.append(message.event)
        original(message)

    with patch.object(coordinator, "async_handle_message", handle):
        stream.push("boom", "{}", "9")
        stream.push("vocab.unknown", "{}", "10")
        await _settle(hass)
    assert handled == ["vocab.unknown"]
    assert stream.calls == [None]


@pytest.mark.parametrize(
    "error",
    [
        OpenParcelsAuthError(401, "unauthorized", None),
        OpenParcelsScopeError(403, "forbidden", "missing scope read"),
    ],
)
async def test_refused_token_starts_reauth(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
    error: Exception,
) -> None:
    """A 401 or a 403 on the stream stops it and asks for a new token (HA-012)."""
    stream.open_error = error
    stream.queue.put_nowait(None)
    await _settle(hass)
    assert hass.states.get("binary_sensor.open_parcels_event_stream").state == "off"
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_unstable_connection_keeps_backing_off(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
) -> None:
    """Connections that drop at once double the delay each time."""
    stream.queue.put_nowait(OpenParcelsConnectionError("first drop"))
    await _settle(hass)
    stream.queue.put_nowait(OpenParcelsConnectionError("second drop"))
    await _settle(hass)
    assert delays == [
        STREAM_BACKOFF_INITIAL_SECONDS,
        STREAM_BACKOFF_INITIAL_SECONDS * 2,
    ]


async def test_stable_connection_resets_backoff(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
) -> None:
    """A connection that stayed up long enough starts the backoff over."""
    stream.queue.put_nowait(OpenParcelsConnectionError("first drop"))
    await _settle(hass)
    with patch.object(stream_module, "STABLE_SECONDS", -1.0):
        stream.queue.put_nowait(OpenParcelsConnectionError("second drop"))
        await _settle(hass)
    assert delays == [STREAM_BACKOFF_INITIAL_SECONDS, STREAM_BACKOFF_INITIAL_SECONDS]


async def test_first_connect_resyncs_once_open(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    client: MagicMock,
) -> None:
    """The first connection reloads over REST once open, closing the setup gap,
    and continues from the seq the server's `ready` carries."""
    coordinator = loaded.runtime_data.coordinator
    stream.push("ready", json.dumps({"seq": 12}), "12")
    await _settle(hass)
    # One load at setup, one after the stream opened.
    assert client.async_get_parcels.await_count == 2
    assert coordinator.last_event_id == "12"


async def test_server_resync_reloads_and_resets_the_id(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    client: MagicMock,
    delays: list[float],
) -> None:
    """`resync` (the id was too old) reloads everything and continues from its seq."""
    coordinator = loaded.runtime_data.coordinator
    stream.push("vocab.unknown", json.dumps({}), "900")
    await _settle(hass)
    before = client.async_get_parcels.await_count
    stream.push("resync", json.dumps({"seq": 40, "reason": "expired"}), "40")
    await _settle(hass)
    assert client.async_get_parcels.await_count == before + 1
    assert coordinator.last_event_id == "40"
    stream.queue.put_nowait(OpenParcelsConnectionError("reset"))
    await _settle(hass)
    assert stream.calls[-1] == "40"


async def test_down_and_back_are_logged_once(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    stream: FakeStream,
    delays: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One info line when the stream goes down, one when it is back (HA-012)."""
    stream.push("vocab.unknown", json.dumps({}), "7")
    stream.queue.put_nowait(OpenParcelsConnectionError("reset"))
    await _settle(hass)
    stream.open_error = OpenParcelsConnectionError("refused")
    stream.queue.put_nowait(OpenParcelsConnectionError("reset again"))
    await _settle(hass)
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    down = [m for m in infos if "event stream is down" in m]
    back = [m for m in infos if "connected again" in m]
    assert len(down) == 2
    assert len(back) == 2
