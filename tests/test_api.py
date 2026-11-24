"""The HTTP client and the SSE parser."""

from __future__ import annotations

from datetime import UTC, datetime
import re

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.open_parcels import api as api_module
from custom_components.open_parcels.api import (
    OpenParcelsApiError,
    OpenParcelsAuthError,
    OpenParcelsClient,
    OpenParcelsConnectionError,
    OpenParcelsError,
    OpenParcelsNotFoundError,
    OpenParcelsScopeError,
    SseParser,
    normalize_url,
)

BASE = "http://op.example.test:4000"
API = f"{BASE}/api/v1"
TOKEN = "op_" + "fake"


def _client(hass: HomeAssistant) -> OpenParcelsClient:
    return OpenParcelsClient(async_get_clientsession(hass), f"{BASE}/api/v1/", TOKEN)


def test_normalize_url() -> None:
    """Trailing slashes and the API prefix are dropped."""
    assert normalize_url(" http://x:1/ ") == "http://x:1"
    assert normalize_url("http://x:1/api/v1") == "http://x:1"


def test_sse_parser() -> None:
    """Fields, comments, multi-line data and empty dispatches."""
    parser = SseParser()
    lines = [
        "retry: 5000",
        "",
        ": keep-alive",
        "",
        "id: 5",
        "event: parcel.registered",
        'data: {"a":',
        "data: 1}",
        "",
        "data:no-space",
        "",
        "id: bad\0id",
        "unknown: field",
        "",
    ]
    messages = [m for line in lines if (m := parser.feed_line(line)) is not None]
    assert len(messages) == 2
    assert messages[0].id == "5"
    assert messages[0].event == "parcel.registered"
    assert messages[0].data == '{"a":\n1}'
    assert messages[1].event == "message"
    assert messages[1].data == "no-space"
    assert messages[1].id is None


def test_sse_event_without_data_dispatches_nothing() -> None:
    """An `event:` with no `data:` is dropped and does not leak into the next message."""
    parser = SseParser()
    lines = ["event: parcel.registered", "", "data: x", ""]
    messages = [m for line in lines if (m := parser.feed_line(line)) is not None]
    assert [(m.event, m.data) for m in messages] == [("message", "x")]


async def test_requests_send_token_and_parse(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each method hits its route with the Bearer token."""
    aioclient_mock.get(f"{API}/sources", json=[{"id": "s"}])
    aioclient_mock.get(f"{API}/carriers", json=[{"id": "dhl"}])
    aioclient_mock.get(f"{API}/parcels", json=[{"id": "p"}])
    aioclient_mock.get(f"{API}/calendar", json=[])
    aioclient_mock.post(f"{API}/parcels", json={"id": "p"}, status=201)
    aioclient_mock.patch(f"{API}/parcels/p", json={"id": "p"})
    aioclient_mock.post(f"{API}/sources/s/sync", status=202)
    client = _client(hass)

    assert await client.async_get_sources() == [{"id": "s"}]
    assert await client.async_get_carriers() == [{"id": "dhl"}]
    assert await client.async_get_parcels(include_history=True, limit=5) == [
        {"id": "p"}
    ]
    moment = datetime(2030, 1, 1, tzinfo=UTC)
    assert await client.async_get_calendar(moment, moment) == []
    assert await client.async_track_parcel(
        "TEST0001", carrier="dhl", label="Shoes"
    ) == {"id": "p"}
    assert await client.async_archive_parcel("p") == {"id": "p"}
    assert await client.async_sync_source("s") is None

    for _, url, data, headers in aioclient_mock.mock_calls:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        if str(url).endswith("/parcels") and data:
            assert data == {
                "barcode": "TEST0001",
                "direction": "incoming",
                "carrier": "dhl",
                "label": "Shoes",
            }


async def test_parcels_follow_the_next_offset(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Every page of GET /parcels is read, as X-Next-Offset points on (HA-004)."""
    aioclient_mock.get(
        f"{API}/parcels?offset=2", json=[{"id": "c"}], headers={"X-Next-Offset": "2"}
    )
    aioclient_mock.get(
        f"{API}/parcels?offset=0",
        json=[{"id": "a"}, {"id": "b"}],
        headers={"X-Next-Offset": "2"},
    )
    client = _client(hass)
    parcels = await client.async_get_parcels(include_history=False, limit=2)
    assert [parcel["id"] for parcel in parcels] == ["a", "b", "c"]
    offsets = [str(url.query["offset"]) for _, url, _, _ in aioclient_mock.mock_calls]
    # The last page's header does not move on, so reading stops there.
    assert offsets == ["0", "2"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, OpenParcelsAuthError),
        (403, OpenParcelsScopeError),
        (404, OpenParcelsNotFoundError),
        (500, OpenParcelsApiError),
    ],
)
async def test_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[Exception],
) -> None:
    """HTTP errors map to typed exceptions, with the server's message."""
    aioclient_mock.get(
        f"{API}/sources", status=status, json={"error": "code", "message": "text"}
    )
    with pytest.raises(error) as info:
        await _client(hass).async_get_sources()
    assert isinstance(info.value, OpenParcelsApiError)
    assert info.value.code == "code"
    assert info.value.message == "text"


async def test_error_without_json_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-JSON error body still raises cleanly."""
    aioclient_mock.get(f"{API}/sources", status=502, text="Bad gateway")
    with pytest.raises(OpenParcelsApiError) as info:
        await _client(hass).async_get_sources()
    assert info.value.code is None


async def test_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Network errors become OpenParcelsConnectionError."""
    aioclient_mock.get(f"{API}/sources", exc=aiohttp.ClientConnectionError("down"))
    with pytest.raises(OpenParcelsConnectionError):
        await _client(hass).async_get_sources()


async def test_validate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Read is checked by listing sources, write by a sync that must 404."""
    aioclient_mock.get(f"{API}/sources", json=[])
    aioclient_mock.post(
        re.compile(rf"{API}/sources/.+/sync"),
        status=404,
        json={"error": "not_found"},
    )
    await _client(hass).async_validate()


async def test_validate_without_write_scope(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A read-only token fails validation with a scope error."""
    aioclient_mock.get(f"{API}/sources", json=[])
    aioclient_mock.post(
        re.compile(rf"{API}/sources/.+/sync"),
        status=403,
        json={"error": "forbidden"},
    )
    with pytest.raises(OpenParcelsScopeError):
        await _client(hass).async_validate()


class _FakeContent:
    """Chunks as the network delivers them; they need not end at a line break."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    async def iter_any(self):
        for line in self._lines:
            yield line


class _FakeResponse:
    def __init__(self, status: int, lines: list[bytes]) -> None:
        self.status = status
        self.content = _FakeContent(lines)

    async def text(self) -> str:
        return '{"error": "unauthorized"}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.headers: dict[str, str] | None = None

    def get(self, url: str, *, headers: dict[str, str], timeout) -> _FakeResponse:
        self.headers = headers
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


async def test_stream_events() -> None:
    """The stream sends Last-Event-ID, calls on_open and yields parsed messages."""
    session = _FakeSession(
        _FakeResponse(
            200,
            [
                b"retry: 5000\n",
                b"\n",
                b"id: 3\n",
                b"event: x\n",
                b"data: {}\r\n",
                b"\n",
            ],
        )
    )
    client = OpenParcelsClient(session, BASE, TOKEN)  # type: ignore[arg-type]
    opened: list[bool] = []
    messages = [
        m
        async for m in client.async_stream_events("2", 90, lambda: opened.append(True))
    ]
    assert opened == [True]
    assert session.headers["Last-Event-ID"] == "2"
    assert [(m.id, m.event, m.data) for m in messages] == [("3", "x", "{}")]


async def test_stream_survives_long_lines_and_split_chunks() -> None:
    """A line over 128 KiB is read whole, and chunks may cut lines anywhere."""
    long_data = "x" * 300_000
    body = f"id: 4\nevent: big\ndata: {long_data}\n\nid: 5\nevent: y\ndata: {{}}\n\n".encode()
    chunks = [body[i : i + 7_000] for i in range(0, len(body), 7_000)]
    client = OpenParcelsClient(_FakeSession(_FakeResponse(200, chunks)), BASE, TOKEN)  # type: ignore[arg-type]
    messages = [m async for m in client.async_stream_events(None, 90)]
    assert [(m.id, m.event, len(m.data)) for m in messages] == [
        ("4", "big", 300_000),
        ("5", "y", 2),
    ]


async def test_stream_drops_a_line_past_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line past the cap is dropped; the stream goes on with the next one."""
    monkeypatch.setattr(api_module, "MAX_SSE_LINE_BYTES", 100)
    body = (
        b"id: 1\nevent: big\ndata: " + b"x" * 500 + b"\n\nid: 2\nevent: y\ndata: {}\n\n"
    )
    chunks = [body[i : i + 50] for i in range(0, len(body), 50)]
    client = OpenParcelsClient(_FakeSession(_FakeResponse(200, chunks)), BASE, TOKEN)  # type: ignore[arg-type]
    messages = [m async for m in client.async_stream_events(None, 90)]
    assert [m.id for m in messages if m.event == "y"] == ["2"]
    # The cut message still arrives, marked, so its change can be reloaded (HA-032).
    assert [(m.id, m.event, m.truncated) for m in messages] == [
        ("1", "big", True),
        ("2", "y", False),
    ]


async def test_non_json_answer_is_a_client_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 200 that is not JSON raises OpenParcelsError, not a bare JSONDecodeError."""
    aioclient_mock.get(f"{API}/sources", text="<html>proxy</html>")
    with pytest.raises(OpenParcelsError):
        await _client(hass).async_get_sources()


async def test_stream_errors() -> None:
    """A 401 and a network error surface as typed exceptions."""
    client = OpenParcelsClient(_FakeSession(_FakeResponse(401, [])), BASE, TOKEN)  # type: ignore[arg-type]
    with pytest.raises(OpenParcelsAuthError):
        async for _ in client.async_stream_events(None, 90):
            pass
    client = OpenParcelsClient(
        _FakeSession(aiohttp.ClientConnectionError("down")),  # type: ignore[arg-type]
        BASE,
        TOKEN,
    )
    with pytest.raises(OpenParcelsConnectionError):
        async for _ in client.async_stream_events(None, 90):
            pass


async def test_returns_route(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """GET /returns asks for the open ones; a server without returns gives none."""
    aioclient_mock.get(f"{API}/returns", json=[{"id": "r"}])
    client = _client(hass)
    assert await client.async_get_returns() == [{"id": "r"}]
    assert str(aioclient_mock.mock_calls[-1][1]).endswith("/returns?view=open")

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/returns", status=404, json={"error": "not_found"})
    assert await client.async_get_returns() == []


async def test_ids_are_escaped_in_paths(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An id cannot change the route it is sent to (HA-020)."""
    aioclient_mock.post(f"{API}/sources/a%2F..%2Fb/sync", status=202)
    await _client(hass).async_sync_source("a/../b")
    assert str(aioclient_mock.mock_calls[-1][1]).endswith("/sources/a%2F..%2Fb/sync")


async def test_parcel_picture(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The picture comes as bytes; a parcel without pictures gives None."""
    aioclient_mock.get(f"{API}/parcels/p/picture", content=b"\x89PNG made up")
    aioclient_mock.get(f"{API}/parcels/q/picture", status=404, json={})
    client = _client(hass)
    assert await client.async_get_parcel_picture("p") == b"\x89PNG made up"
    assert await client.async_get_parcel_picture("q") is None
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
