"""pytest configuration for the Open Parcels tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Generator
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.api import SseMessage
from custom_components.open_parcels.const import CONF_API_TOKEN, DOMAIN

from . import payloads

URL = "http://open-parcels.example.test:4000"
TOKEN = "op_" + "made_up_token"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom integration loadable."""


class FakeStream:
    """Stands in for the event stream: tests push messages, None closes it."""

    def __init__(self) -> None:
        """Start with an empty queue."""
        self.queue: asyncio.Queue[SseMessage | BaseException | None] = asyncio.Queue()
        self.calls: list[str | None] = []
        self.open_error: BaseException | None = None

    def push(self, event: str, data: str, event_id: str | None = None) -> None:
        """Queue one message."""
        self.queue.put_nowait(SseMessage(event=event, data=data, id=event_id))

    async def __call__(
        self,
        last_event_id: str | None,
        read_timeout: float,
        on_open: Callable[[], None] | None = None,
    ) -> AsyncIterator[SseMessage]:
        """Behave like OpenParcelsClient.async_stream_events."""
        self.calls.append(last_event_id)
        if self.open_error is not None:
            error, self.open_error = self.open_error, None
            raise error
        if on_open is not None:
            on_open()
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item


@pytest.fixture
def stream() -> FakeStream:
    """The fake event stream."""
    return FakeStream()


@pytest.fixture
def client(stream: FakeStream) -> Generator[MagicMock]:
    """A mocked client used by the integration and the config flow."""
    mock = MagicMock()
    mock.base_url = URL
    mock.async_get_sources = AsyncMock(
        side_effect=lambda: [dict(s) for s in payloads.SOURCES]
    )
    mock.async_get_carriers = AsyncMock(return_value=payloads.CARRIERS)
    mock.async_get_parcels = AsyncMock(side_effect=lambda **_: payloads.parcels())
    mock.async_get_returns = AsyncMock(side_effect=lambda: payloads.returns())
    mock.async_get_calendar = AsyncMock(
        side_effect=lambda *_: payloads.calendar_entries()
    )
    mock.async_track_parcel = AsyncMock(
        return_value={"id": payloads.PARCEL_A, "carrier": "dhl"}
    )
    mock.async_archive_parcel = AsyncMock(return_value={})
    mock.async_sync_source = AsyncMock(return_value=None)
    mock.async_validate = AsyncMock(return_value=None)
    mock.async_stream_events = stream
    with (
        patch("custom_components.open_parcels.OpenParcelsClient", return_value=mock),
        patch(
            "custom_components.open_parcels.config_flow.OpenParcelsClient",
            return_value=mock,
        ),
    ):
        yield mock


@pytest.fixture
def entry() -> MockConfigEntry:
    """A config entry with a unique id, as real ones have."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Open Parcels",
        unique_id=URL,
        data={CONF_URL: URL, CONF_API_TOKEN: TOKEN},
        options={},
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> MockConfigEntry:
    """The entry, set up."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def events(hass: HomeAssistant, event_type: str) -> list[Any]:
    """Collect bus events of a type."""
    collected: list[Any] = []
    hass.bus.async_listen(event_type, collected.append)
    return collected


if sys.platform == "win32":
    # pytest-homeassistant-custom-component blocks socket creation, but Windows
    # event loops build their self-pipe from AF_INET sockets. Keep the
    # connect-time allowlist and drop only the creation block (as the suite does).
    import pytest_socket

    pytest_socket.disable_socket = lambda allow_unix_socket=False: None

    from aiohttp.resolver import ThreadedResolver
    import homeassistant.helpers.aiohttp_client as _ha_aiohttp_client

    _ha_aiohttp_client.AsyncResolver = ThreadedResolver
