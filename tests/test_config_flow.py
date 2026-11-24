"""Config flow: user, discovery, reauth, reconfigure, options."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_parcels.api import (
    OpenParcelsApiError,
    OpenParcelsAuthError,
    OpenParcelsConnectionError,
    OpenParcelsScopeError,
)
from custom_components.open_parcels.const import CONF_API_TOKEN, DOMAIN

from .conftest import TOKEN, URL

NEW_TOKEN = "op_" + "another_made_up_token"


async def test_user_flow(hass: HomeAssistant, client: MagicMock) -> None:
    """URL and token create the entry; the URL is normalised."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{URL}/api/v1/", CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL, CONF_API_TOKEN: TOKEN}
    assert result["result"].unique_id == URL


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (OpenParcelsAuthError(401, None, None), "invalid_auth"),
        (OpenParcelsScopeError(403, None, None), "missing_scope"),
        (OpenParcelsConnectionError("down"), "cannot_connect"),
        (OpenParcelsApiError(500, None, None), "unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, client: MagicMock, error: Exception, key: str
) -> None:
    """Validation errors show on the form, and the user can retry."""
    client.async_validate.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_TOKEN: TOKEN}
    )
    assert result["errors"] == {"base": key}
    client.async_validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "url",
    [
        "open-parcels.example.test:4000",
        "ftp://open-parcels.example.test",
        "http://user:secret@open-parcels.example.test",
        "http://open-parcels.example.test/?a=1",
        "http://open-parcels.example.test/#top",
        "http://",
    ],
)
async def test_user_flow_rejects_odd_urls(
    hass: HomeAssistant, client: MagicMock, url: str
) -> None:
    """Only plain http(s) addresses are taken (HA-020)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_API_TOKEN: TOKEN}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    client.async_validate.assert_not_awaited()


async def test_user_flow_already_configured(
    hass: HomeAssistant, client: MagicMock, entry: MockConfigEntry
) -> None:
    """The same server cannot be added twice."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_hassio_discovery_with_token(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """The app can hand over a token: the user only confirms (HA-011)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"host": "local-open-parcels", "port": 3000, CONF_API_TOKEN: TOKEN},
            name="Open Parcels",
            slug="open_parcels",
            uuid="1234",
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: "http://local-open-parcels:3000",
        CONF_API_TOKEN: TOKEN,
    }


async def test_hassio_discovery_without_host_aborts(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Malformed discovery data aborts instead of failing."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"port": 3000}, name="Open Parcels", slug="open_parcels", uuid="1"
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_hassio_discovery_asks_for_token(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Without a token the user is asked for one."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"host": "local-open-parcels", "port": 3000, "ssl": True},
            name="Open Parcels",
            slug="open_parcels",
            uuid="1234",
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    client.async_validate.side_effect = OpenParcelsAuthError(401, None, None)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "op_wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    client.async_validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == "https://local-open-parcels:3000"


async def test_discovery_of_configured_server_aborts(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A discovered server that is already set up aborts."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://local-open-parcels:3000",
        data={CONF_URL: "http://local-open-parcels:3000", CONF_API_TOKEN: TOKEN},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"host": "local-open-parcels", "port": 3000},
            name="Open Parcels",
            slug="open_parcels",
            uuid="1234",
        ),
    )
    assert result["type"] is FlowResultType.ABORT


async def test_hassio_rediscovery_updates_the_token(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """The app announcing itself again with a new token updates the entry (AP-002)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://local-open-parcels:3000",
        data={CONF_URL: "http://local-open-parcels:3000", CONF_API_TOKEN: TOKEN},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={
                "host": "local-open-parcels",
                "port": 3000,
                CONF_API_TOKEN: "op_made_up_new_token",
            },
            name="Open Parcels",
            slug="open_parcels",
            uuid="1234",
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_API_TOKEN] == "op_made_up_new_token"
    assert entry.data[CONF_URL] == "http://local-open-parcels:3000"


async def test_reauth(
    hass: HomeAssistant, client: MagicMock, loaded: MockConfigEntry
) -> None:
    """A new token is stored and the entry reloads."""
    result = await loaded.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    client.async_validate.side_effect = OpenParcelsScopeError(403, None, None)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: NEW_TOKEN}
    )
    assert result["errors"] == {"base": "missing_scope"}
    client.async_validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: NEW_TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert loaded.data[CONF_API_TOKEN] == NEW_TOKEN


async def test_reconfigure(
    hass: HomeAssistant, client: MagicMock, loaded: MockConfigEntry
) -> None:
    """URL and token can change; the unique id follows the URL."""
    result = await loaded.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "new.example.test", CONF_API_TOKEN: TOKEN}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    client.async_validate.side_effect = OpenParcelsConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "http://new.example.test:4000", CONF_API_TOKEN: TOKEN},
    )
    assert result["errors"] == {"base": "cannot_connect"}
    client.async_validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "http://new.example.test:4000", CONF_API_TOKEN: TOKEN},
    )
    assert result["reason"] == "reconfigure_successful"
    assert loaded.data[CONF_URL] == "http://new.example.test:4000"
    assert loaded.unique_id == "http://new.example.test:4000"


async def test_reconfigure_to_other_configured_server(
    hass: HomeAssistant, client: MagicMock, loaded: MockConfigEntry
) -> None:
    """Pointing an entry at another entry's server aborts."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://other.example.test",
        data={CONF_URL: "http://other.example.test", CONF_API_TOKEN: TOKEN},
    ).add_to_hass(hass)
    result = await loaded.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "http://other.example.test", CONF_API_TOKEN: TOKEN},
    )
    assert result["reason"] == "already_configured"


async def test_options(
    hass: HomeAssistant, client: MagicMock, loaded: MockConfigEntry
) -> None:
    """Options are stored as integers and reload the entry."""
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "resync_minutes": 60.0,
            "include_history": True,
            "order_entities": True,
            "delivered_filter_type": "parcels",
            "delivered_filter_amount": 5.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded.options == {
        "resync_minutes": 60,
        "include_history": True,
        "order_entities": True,
        "delivered_filter_type": "parcels",
        "delivered_filter_amount": 5,
    }
    await hass.async_block_till_done()
    assert loaded.runtime_data.coordinator.update_interval.total_seconds() == 3600
