"""Config flow: server URL and API token, discovery, reauth, reconfigure, options."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import (
    OpenParcelsAuthError,
    OpenParcelsClient,
    OpenParcelsConnectionError,
    OpenParcelsError,
    OpenParcelsScopeError,
    checked_url,
    normalize_url,
)
from .const import (
    CONF_API_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_ORDER_ENTITIES,
    CONF_RESYNC_MINUTES,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_ORDER_ENTITIES,
    DEFAULT_RESYNC_MINUTES,
    DOMAIN,
    MAX_RESYNC_MINUTES,
    MIN_RESYNC_MINUTES,
)
from .device import HUB_NAME
from .schema import vol

_LOGGER = logging.getLogger(__name__)

_TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def _user_schema(url: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=url): _URL_SELECTOR,
            vol.Required(CONF_API_TOKEN): _TOKEN_SELECTOR,
        }
    )


_TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_API_TOKEN): _TOKEN_SELECTOR})


class OpenParcelsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up an Open Parcels server."""

    VERSION = 2

    def __init__(self) -> None:
        """Start without a discovered server."""
        self._discovered_url: str | None = None
        self._discovered_token: str | None = None

    async def _validate(self, url: str, token: str) -> str | None:
        """Return an error key, or None when the token can read and write."""
        client = OpenParcelsClient(async_get_clientsession(self.hass), url, token)
        try:
            await client.async_validate()
        except OpenParcelsAuthError:
            return "invalid_auth"
        except OpenParcelsScopeError:
            return "missing_scope"
        except OpenParcelsConnectionError:
            return "cannot_connect"
        except OpenParcelsError:
            _LOGGER.exception("Unexpected answer while validating the server")
            return "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the URL and the token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = checked_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                # A flow the user started takes precedence over a leftover one for the
                # same server.
                await self.async_set_unique_id(url, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                error = await self._validate(url, user_input[CONF_API_TOKEN])
                if error is None:
                    return self.async_create_entry(
                        title=HUB_NAME,
                        data={
                            CONF_URL: url,
                            CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                        },
                    )
                errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(), user_input),
            errors=errors,
        )

    async def _async_discovered(self, url: str, token: str | None) -> ConfigFlowResult:
        url = normalize_url(url)
        await self.async_set_unique_id(url)
        # The HA app hands over its token again whenever it announces itself: keep the
        # entry on the token it uses now (the entry reloads when either changed).
        updates = {CONF_URL: url}
        if token:
            updates[CONF_API_TOKEN] = token
        self._abort_if_unique_id_configured(updates=updates)
        self._discovered_url = url
        self.context["title_placeholders"] = {"url": url}
        if token and await self._validate(url, token) is None:
            self._discovered_token = token
            return await self.async_step_hassio_confirm()
        return await self.async_step_discovery_confirm()

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """The Open Parcels HA app announced itself."""
        config = discovery_info.config
        host, port = config.get("host"), config.get("port")
        if not host or not port:
            return self.async_abort(reason="invalid_discovery_info")
        scheme = "https" if config.get("ssl") else "http"
        return await self._async_discovered(
            f"{scheme}://{host}:{port}", config.get(CONF_API_TOKEN)
        )

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The HA app handed over a working token: the user still confirms the entry."""
        assert self._discovered_url is not None
        assert self._discovered_token is not None
        if user_input is not None:
            return self.async_create_entry(
                title=HUB_NAME,
                data={
                    CONF_URL: self._discovered_url,
                    CONF_API_TOKEN: self._discovered_token,
                },
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"url": self._discovered_url},
        )

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a token for the discovered server."""
        assert self._discovered_url is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._validate(
                self._discovered_url, user_input[CONF_API_TOKEN]
            )
            if error is None:
                return self.async_create_entry(
                    title=HUB_NAME,
                    data={
                        CONF_URL: self._discovered_url,
                        CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                    },
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=_TOKEN_SCHEMA,
            description_placeholders={"url": self._discovered_url},
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The token was refused."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._validate(
                entry.data[CONF_URL], user_input[CONF_API_TOKEN]
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_TOKEN_SCHEMA,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def _reconfigure(
        self, entry: ConfigEntry, user_input: dict[str, Any]
    ) -> dict[str, str] | None:
        """Check a changed URL and token; the form's errors, or None."""
        try:
            url = checked_url(user_input[CONF_URL])
        except ValueError:
            return {CONF_URL: "invalid_url"}
        if url != entry.unique_id:
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
        error = await self._validate(url, user_input[CONF_API_TOKEN])
        return None if error is None else {"base": error}

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL or the token."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._reconfigure(entry, user_input)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=normalize_url(user_input[CONF_URL]),
                    data_updates={
                        CONF_URL: normalize_url(user_input[CONF_URL]),
                        CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                    },
                )
            errors.update(error)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(entry.data[CONF_URL]),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenParcelsOptionsFlow:
        """The options flow."""
        return OpenParcelsOptionsFlow()


class OpenParcelsOptionsFlow(OptionsFlowWithReload):
    """Resync interval, history attribute and delivered-parcel retention."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **user_input,
                    CONF_RESYNC_MINUTES: int(user_input[CONF_RESYNC_MINUTES]),
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RESYNC_MINUTES,
                    default=options.get(CONF_RESYNC_MINUTES, DEFAULT_RESYNC_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_RESYNC_MINUTES,
                        max=MAX_RESYNC_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                vol.Required(
                    CONF_INCLUDE_HISTORY,
                    default=options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
                ): BooleanSelector(),
                vol.Required(
                    CONF_ORDER_ENTITIES,
                    default=options.get(CONF_ORDER_ENTITIES, DEFAULT_ORDER_ENTITIES),
                ): BooleanSelector(),
                vol.Required(
                    CONF_DELIVERED_FILTER_TYPE,
                    default=options.get(
                        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["days", "parcels"],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_DELIVERED_FILTER_TYPE,
                    )
                ),
                vol.Required(
                    CONF_DELIVERED_FILTER_AMOUNT,
                    default=options.get(
                        CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=365, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
