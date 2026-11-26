"""Client for the Open Parcels REST API and its Server-Sent Events stream."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from typing import Any
from urllib.parse import quote
import uuid

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class OpenParcelsError(Exception):
    """Base error for the Open Parcels client."""


class OpenParcelsConnectionError(OpenParcelsError):
    """The server could not be reached."""


class OpenParcelsApiError(OpenParcelsError):
    """The server answered with an error."""

    def __init__(self, status: int, code: str | None, message: str | None) -> None:
        """Initialise with the HTTP status and the server's error body."""
        super().__init__(f"HTTP {status}: {code or 'error'}: {message or ''}".rstrip())
        self.status = status
        self.code = code
        self.message = message


class OpenParcelsAuthError(OpenParcelsApiError):
    """The token was refused (401)."""


class OpenParcelsScopeError(OpenParcelsApiError):
    """The token lacks a scope (403)."""


class OpenParcelsNotFoundError(OpenParcelsApiError):
    """The resource does not exist (404)."""


@dataclass(frozen=True, slots=True)
class SseMessage:
    """One message from the event stream."""

    event: str
    data: str
    id: str | None
    # A line of this message was over MAX_SSE_LINE_BYTES and dropped: its data is incomplete.
    truncated: bool = False


class SseParser:
    """Incremental parser for the text/event-stream format."""

    def __init__(self) -> None:
        """Start with an empty message."""
        self._event = ""
        self._data: list[str] = []
        self._id: str | None = None
        self._truncated = False

    def mark_dropped(self) -> None:
        """A line of the current message was dropped; the message is incomplete."""
        self._truncated = True

    def feed_line(self, line: str) -> SseMessage | None:
        """Consume one line (without its terminator); return a message when one ends."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        elif field == "id" and "\0" not in value:
            self._id = value
        return None

    def _dispatch(self) -> SseMessage | None:
        if not self._data and not self._truncated:
            # The SSE spec: an empty data buffer dispatches nothing, even after `event:`.
            self._event = ""
            self._id = None
            return None
        message = SseMessage(
            event=self._event or "message",
            data="\n".join(self._data),
            id=self._id,
            truncated=self._truncated,
        )
        self._event = ""
        self._data = []
        self._id = None
        self._truncated = False
        return message


def normalize_url(url: str) -> str:
    """Return the server's base URL without a trailing slash or API prefix."""
    base = url.strip().rstrip("/")
    return base.removesuffix(API_PREFIX)


def checked_url(url: str) -> str:
    """The normalised base URL; ValueError unless it is a plain http(s) address.

    Credentials, a query or a fragment in the address would leak into the device
    page and error messages, or silently change every request.
    """
    base = normalize_url(url)
    try:
        parsed = URL(base)
    except ValueError as err:
        raise ValueError("not a URL") from err
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError("needs http:// or https:// and a host")
    if parsed.user or parsed.password or parsed.query_string or parsed.fragment:
        raise ValueError("no credentials, query or fragment allowed")
    return base


def _segment(value: str) -> str:
    """One path segment, escaped: ids from events never change the route."""
    return quote(value, safe="")


class OpenParcelsClient:
    """Thin async client; every method maps to one API route."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        """Initialise with a shared session, the server URL and a Bearer token."""
        self._session = session
        self.base_url = normalize_url(url)
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        answer, _ = await self._exchange(method, path, params=params, body=body)
        return answer

    async def _exchange(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        binary: bool = False,
    ) -> tuple[Any, Mapping[str, str]]:
        """One request: the decoded answer (bytes when binary) and the headers."""
        url = f"{self.base_url}{API_PREFIX}{path}"
        headers = self._headers
        if binary:
            headers = {"Authorization": headers["Authorization"]}
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=body,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status >= 400:
                    await _raise_for_status(response)
                if binary:
                    return await response.read(), response.headers
                if response.status in (202, 204):
                    return None, response.headers
                try:
                    return await response.json(content_type=None), response.headers
                except ValueError as err:
                    # A proxy's HTML page or a cut-off body: the server's answer, not JSON.
                    raise OpenParcelsError(
                        f"The server's answer to {path} is not JSON"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OpenParcelsConnectionError(str(err) or type(err).__name__) from err

    async def async_get_parcel_picture(self, parcel_id: str) -> bytes | None:
        """GET /parcels/{id}/picture: the item pictures as one PNG; None without any."""
        try:
            png, _ = await self._exchange(
                "GET", f"/parcels/{_segment(parcel_id)}/picture", binary=True
            )
        except OpenParcelsNotFoundError:
            return None
        return bytes(png)

    async def async_get_connections(self) -> list[dict[str, Any]]:
        """GET /connections."""
        return list(await self._request("GET", "/connections"))

    async def async_get_carriers(self) -> list[dict[str, Any]]:
        """GET /carriers."""
        return list(await self._request("GET", "/carriers"))

    async def _get_pages(
        self, path: str, params: dict[str, str], limit: int
    ) -> list[dict[str, Any]]:
        """GET a list route, following the pages.

        A full page carries `X-Next-Offset`, the offset of the next one.
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = {**params, "limit": str(limit), "offset": str(offset)}
            page, headers = await self._exchange("GET", path, params=query)
            rows.extend(page)
            following = headers.get("X-Next-Offset", "")
            # Only forward: a header that does not move on would loop forever.
            if not following.isdigit() or int(following) <= offset:
                return rows
            offset = int(following)

    async def async_get_parcels(
        self, *, include_history: bool, limit: int
    ) -> list[dict[str, Any]]:
        """GET /parcels: every parcel that is not archived."""
        history = "true" if include_history else "false"
        return await self._get_pages("/parcels", {"include_history": history}, limit)

    async def async_get_orders(self, *, limit: int) -> list[dict[str, Any]]:
        """GET /orders: the open orders, with their totals and status."""
        return await self._get_pages("/orders", {"view": "open"}, limit)

    async def async_get_status(self) -> dict[str, Any]:
        """GET /status: the state of every part of the server (read scope)."""
        return dict(await self._request("GET", "/status"))

    async def async_get_backups(self) -> dict[str, Any]:
        """GET /data/backups: the newest backup and the ones kept (admin scope)."""
        return dict(await self._request("GET", "/data/backups"))

    async def async_back_up(self) -> dict[str, Any]:
        """POST /data/backups: back the database up now (admin scope)."""
        return dict(await self._request("POST", "/data/backups"))

    async def async_get_calendar(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """GET /calendar for a range."""
        params = {"from": start.isoformat(), "to": end.isoformat()}
        return list(await self._request("GET", "/calendar", params=params))

    async def async_get_returns(self) -> list[dict[str, Any]]:
        """GET /returns: the open returns; empty on a server without returns."""
        try:
            return list(await self._request("GET", "/returns", params={"view": "open"}))
        except OpenParcelsNotFoundError:
            return []

    async def async_track_parcel(
        self,
        tracking_number: str,
        *,
        carrier: str | None = None,
        label: str | None = None,
        direction: str = "receiving",
    ) -> dict[str, Any]:
        """POST /parcels: start tracking a code."""
        body: dict[str, Any] = {
            "tracking_number": tracking_number,
            "direction": direction,
        }
        if carrier:
            body["carrier"] = carrier
        if label:
            body["label"] = label
        return dict(await self._request("POST", "/parcels", body=body))

    async def async_archive_parcel(self, parcel_id: str) -> dict[str, Any]:
        """PATCH /parcels/{id} with archived=true."""
        return dict(
            await self._request(
                "PATCH", f"/parcels/{_segment(parcel_id)}", body={"archived": True}
            )
        )

    async def async_sync_connection(self, connection_id: str) -> None:
        """POST /connections/{id}/sync."""
        await self._request("POST", f"/connections/{_segment(connection_id)}/sync")

    async def async_get_jobs(self) -> list[dict[str, Any]]:
        """GET /jobs: every connection's jobs, newest first."""
        return list(await self._request("GET", "/jobs"))

    async def async_run_action(
        self, connection_id: str, action: str, action_input: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /connections/{id}/actions/{action}: `{job}` for a task, `{url}` for a link."""
        path = f"/connections/{_segment(connection_id)}/actions/{_segment(action)}"
        return dict(
            await self._request("POST", path, body={"input": action_input}) or {}
        )

    async def async_cancel_job(self, job_id: str) -> dict[str, Any]:
        """POST /jobs/{id}/cancel."""
        return dict(await self._request("POST", f"/jobs/{_segment(job_id)}/cancel"))

    async def async_validate(self) -> None:
        """Check the token can read and write, without changing anything.

        Reading is checked by listing the connections. Writing is checked by asking
        to sync a connection that cannot exist: a token with the write scope gets
        404, one without it gets 403.
        """
        await self.async_get_connections()
        try:
            await self.async_sync_connection(str(uuid.uuid4()))
        except OpenParcelsNotFoundError:
            return

    async def async_stream_events(
        self,
        last_event_id: str | None,
        read_timeout: float,
        on_open: Callable[[], None] | None = None,
    ) -> AsyncIterator[SseMessage]:
        """Yield messages from GET /events until the server closes the stream.

        ``on_open`` runs once the server has accepted the stream.
        """
        headers = {**self._headers, "Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=read_timeout
        )
        parser = SseParser()
        try:
            async with self._session.get(
                f"{self.base_url}{API_PREFIX}/events", headers=headers, timeout=timeout
            ) as response:
                if response.status >= 400:
                    await _raise_for_status(response)
                if on_open is not None:
                    on_open()
                # Lines are split here, not by aiohttp's readline: that one raises "Chunk too
                # big" on a line over 128 KiB (a parcel with a long history), which killed the
                # stream. A line over MAX_SSE_LINE_BYTES is dropped and the stream goes on.
                async for line in _sse_lines(response.content):
                    if line is None:
                        parser.mark_dropped()
                        continue
                    message = parser.feed_line(line)
                    if message is not None:
                        yield message
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OpenParcelsConnectionError(str(err) or type(err).__name__) from err


# The longest event line kept; a longer one is dropped instead of ending the stream.
MAX_SSE_LINE_BYTES = 8 * 1024 * 1024


async def _sse_lines(content: aiohttp.StreamReader) -> AsyncIterator[str | None]:
    """The stream's lines, without their line ending, split on LF (a CR before it is dropped).

    None stands for a line dropped for its size.
    """
    buffer = bytearray()
    skipping = False
    async for chunk in content.iter_any():
        buffer.extend(chunk)
        while (end := buffer.find(b"\n")) != -1:
            raw = bytes(buffer[:end])
            del buffer[: end + 1]
            if skipping:
                skipping = False
                continue
            yield raw.decode("utf-8", errors="replace").rstrip("\r")
        if len(buffer) > MAX_SSE_LINE_BYTES:
            _LOGGER.warning(
                "An event stream line exceeded %s bytes; dropped", MAX_SSE_LINE_BYTES
            )
            buffer.clear()
            if not skipping:
                yield None
            skipping = True
    if buffer and not skipping:
        yield bytes(buffer).decode("utf-8", errors="replace").rstrip("\r")


async def _raise_for_status(response: aiohttp.ClientResponse) -> None:
    code: str | None = None
    message: str | None = None
    try:
        body = json.loads(await response.text())
    except (ValueError, aiohttp.ClientError):
        body = None
    if isinstance(body, dict):
        code = body.get("error") if isinstance(body.get("error"), str) else None
        message = body.get("message") if isinstance(body.get("message"), str) else None
    status = response.status
    if status == 401:
        raise OpenParcelsAuthError(status, code, message)
    if status == 403:
        raise OpenParcelsScopeError(status, code, message)
    if status == 404:
        raise OpenParcelsNotFoundError(status, code, message)
    raise OpenParcelsApiError(status, code, message)
