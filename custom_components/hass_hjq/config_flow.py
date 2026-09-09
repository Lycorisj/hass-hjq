"""Config flow for 移动爱家（原和家亲）."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BITRATE_1500K,
    BITRATE_2500K,
    BITRATE_800K,
    CONF_AUDIO,
    CONF_BITRATE,
    CONF_RECORD_SEGMENT,
    CONF_SCALE,
    CONF_TRANSCODE,
    DEFAULT_AUDIO,
    DEFAULT_BITRATE,
    DEFAULT_RECORD_SEGMENT,
    DEFAULT_SCALE,
    DEFAULT_TRANSCODE,
    DOMAIN,
    SCALE_1080P,
    SCALE_720P,
    SCALE_SOURCE,
)
from .hjqapi import HJQApi, HJQAuthError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

SCALE_OPTIONS = [SCALE_720P, SCALE_1080P, SCALE_SOURCE]
BITRATE_OPTIONS = [BITRATE_800K, BITRATE_1500K, BITRATE_2500K]


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate credentials by performing a real login."""
    session = async_get_clientsession(hass)
    api = HJQApi(tel=data[CONF_USERNAME], pwd=data[CONF_PASSWORD], session=session)
    try:
        await api.get_hjqtoken_passid()
        await api.get_video_auth()
    except HJQAuthError as err:
        _LOGGER.warning("Login failed: %s", err)
        raise InvalidAuth from err
    return {"title": f"用户: {data[CONF_USERNAME]}"}


class HassHjqConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for 移动爱家."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()
            try:
                info = await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during login")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauthentication when the password no longer works."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt for a new password."""
        errors: dict[str, str] = {}
        reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if reauth_entry is None:
            return self.async_abort(reason="reauth_successful")
        if user_input is not None:
            data = {
                CONF_USERNAME: reauth_entry.data[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                await validate_input(self.hass, data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(reauth_entry, data=data)
                await self.hass.config_entries.async_reload(reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return HassHjqOptionsFlow(config_entry)


class HassHjqOptionsFlow(OptionsFlow):
    """Handle integration options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow (compatible with HA 2024.6 and 2024.12+)."""
        self._config_entry = config_entry

    def _entry(self) -> ConfigEntry:
        return getattr(self, "config_entry", None) or self._config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._entry().options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRANSCODE,
                    default=options.get(CONF_TRANSCODE, DEFAULT_TRANSCODE),
                ): bool,
                vol.Required(
                    CONF_SCALE,
                    default=options.get(CONF_SCALE, DEFAULT_SCALE),
                ): vol.In(SCALE_OPTIONS),
                vol.Required(
                    CONF_BITRATE,
                    default=options.get(CONF_BITRATE, DEFAULT_BITRATE),
                ): vol.In(BITRATE_OPTIONS),
                vol.Required(
                    CONF_AUDIO,
                    default=options.get(CONF_AUDIO, DEFAULT_AUDIO),
                ): bool,
                vol.Required(
                    CONF_RECORD_SEGMENT,
                    default=options.get(CONF_RECORD_SEGMENT, DEFAULT_RECORD_SEGMENT),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
