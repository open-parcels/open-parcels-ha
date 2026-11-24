"""Parcel pictures (a grid of the item pictures) served by Home Assistant itself.

The frontend loads entity pictures without the server's token, so Home Assistant
fetches the picture from the server and serves it under its own address. That
address is signed with a key derived from the entry's token: nobody can make one
up, and the picture is all it gives access to. A change in the items changes the
address, so browsers never keep an outdated picture. Addresses also expire: the
signature covers a time window, and only the current and the previous window are
accepted, so an address copied out of history stops working within a day.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import hmac
from http import HTTPStatus
import time

from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

from .api import OpenParcelsError
from .const import CONF_API_TOKEN, DOMAIN, PICTURE_URL


def _key(token: str) -> bytes:
    return hashlib.sha256(f"{DOMAIN} picture\0{token}".encode()).digest()


def picture_version(image_urls: Iterable[str]) -> str:
    """A short hash of the pictures, so new items give a new address."""
    return hashlib.sha256("\n".join(image_urls).encode()).hexdigest()[:12]


# Length of one signing window; an address works for one to two of them (HA-019).
WINDOW_SECONDS = 12 * 60 * 60


def current_window() -> int:
    """The number of the signing window now."""
    return int(time.time() // WINDOW_SECONDS)


def _signature(
    token: str, entry_id: str, parcel_id: str, version: str, window: int
) -> str:
    message = f"{entry_id}\0{parcel_id}\0{version}\0{window}".encode()
    return hmac.new(_key(token), message, hashlib.sha256).hexdigest()[:32]


def picture_url(token: str, entry_id: str, parcel_id: str, version: str) -> str:
    """The signed address of a parcel's picture, valid for the current window."""
    path = PICTURE_URL.format(entry_id=entry_id, parcel_id=parcel_id)
    window = current_window()
    sig = _signature(token, entry_id, parcel_id, version, window)
    return f"{path}?v={version}&w={window}&sig={sig}"


class ParcelPictureView(HomeAssistantView):
    """GET a parcel's item grid; the signature stands in for a login."""

    url = PICTURE_URL
    name = f"api:{DOMAIN}:picture"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep Home Assistant for the entry lookup."""
        self._hass = hass

    async def get(
        self, request: web.Request, entry_id: str, parcel_id: str
    ) -> web.StreamResponse:
        """Check the signature, then pass the server's PNG on."""
        entry = self._hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        version = request.query.get("v", "")
        window = request.query.get("w", "")
        now = current_window()
        if not window.isdigit() or int(window) not in (now, now - 1):
            return web.Response(status=HTTPStatus.FORBIDDEN)
        expected = _signature(
            entry.data[CONF_API_TOKEN], entry_id, parcel_id, version, int(window)
        )
        if not hmac.compare_digest(request.query.get("sig", ""), expected):
            return web.Response(status=HTTPStatus.FORBIDDEN)
        try:
            png = await entry.runtime_data.client.async_get_parcel_picture(parcel_id)
        except OpenParcelsError:
            return web.Response(status=HTTPStatus.BAD_GATEWAY)
        if png is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.Response(
            body=png,
            content_type="image/png",
            # The address changes with the items and the window, so the picture
            # may be kept for as long as the address lives.
            headers={"Cache-Control": f"private, max-age={WINDOW_SECONDS}"},
        )
