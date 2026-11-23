"""The live event stream: connect, replay with Last-Event-ID, reconnect with backoff."""

from __future__ import annotations

import asyncio
import logging
import random

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import OpenParcelsAuthError, OpenParcelsError, SseMessage
from .const import (
    STREAM_BACKOFF_INITIAL_SECONDS,
    STREAM_BACKOFF_MAX_SECONDS,
    STREAM_READ_TIMEOUT_SECONDS,
)
from .coordinator import OpenParcelsCoordinator

_LOGGER = logging.getLogger(__name__)

# A connection that lasted this long counts as healthy.
STABLE_SECONDS = 60.0


def next_backoff(current: float) -> float:
    """Double the delay up to the cap."""
    return min(current * 2, STREAM_BACKOFF_MAX_SECONDS)


def jittered(delay: float) -> float:
    """Spread reconnects by up to a fifth of the delay."""
    return delay * (1 + random.random() / 5)


class _Attempt:
    """One connection attempt: notes when the server accepted it."""

    def __init__(
        self, hass: HomeAssistant, coordinator: OpenParcelsCoordinator, *, resync: bool
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._resync = resync
        self.opened_at: float | None = None

    def on_open(self) -> None:
        """Mark the stream connected; resync when no Last-Event-ID could be sent."""
        self.opened_at = self._hass.loop.time()
        self._coordinator.async_set_stream_connected(True)
        if self._resync:
            # Directly, not through the debouncer: its cooldown would hold back the
            # next refresh someone asks for (a button, a service call).
            self._coordinator.async_start_refresh(debounced=False)


def _handle(coordinator: OpenParcelsCoordinator, message: SseMessage) -> None:
    """Apply one message; a message that fails is logged and skipped, the stream goes on."""
    try:
        coordinator.async_handle_message(message)
    except Exception:
        _LOGGER.exception("Handling the event stream message %s failed", message.event)


async def async_run_stream(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: OpenParcelsCoordinator
) -> None:
    """Keep the event stream open until the entry unloads.

    The first connection starts without Last-Event-ID: replaying the past would
    re-fire events that happened while Home Assistant was not running. The
    server answers it with `ready` and the current sequence, which becomes the
    last id; a REST resync once the stream is open covers whatever changed
    between the setup's first load and the connect. Reconnects send the last
    id seen so nothing in between is lost; whenever none is known yet, the same
    resync after opening fills the gap. A `resync` message from the server
    (the id was too old, or the server's database was reset) also reloads.
    """
    delay = STREAM_BACKOFF_INITIAL_SECONDS
    while True:
        last_id = coordinator.last_event_id
        attempt = _Attempt(hass, coordinator, resync=last_id is None)
        try:
            async for message in coordinator.client.async_stream_events(
                last_id, STREAM_READ_TIMEOUT_SECONDS, attempt.on_open
            ):
                _handle(coordinator, message)
            _LOGGER.debug("The event stream closed; reconnecting")
        except OpenParcelsAuthError:
            coordinator.async_set_stream_connected(False)
            _LOGGER.warning("The event stream refused the API token")
            entry.async_start_reauth(hass)
            return
        except OpenParcelsError as err:
            _LOGGER.debug("Event stream error: %s", err)
        except asyncio.CancelledError:
            coordinator.async_set_stream_connected(False)
            raise
        except Exception:
            # Anything else (a parser bug, an aiohttp error of a kind not wrapped) must not end
            # the task while the sensor still says connected: log it and reconnect.
            _LOGGER.exception("The event stream failed unexpectedly; reconnecting")
        coordinator.async_set_stream_connected(False)
        # Only a connection that stayed up a while resets the backoff, so a
        # server that accepts and drops at once cannot cause a tight loop.
        if (
            attempt.opened_at is not None
            and hass.loop.time() - attempt.opened_at > STABLE_SECONDS
        ):
            delay = STREAM_BACKOFF_INITIAL_SECONDS
        await asyncio.sleep(jittered(delay))
        delay = next_backoff(delay)
