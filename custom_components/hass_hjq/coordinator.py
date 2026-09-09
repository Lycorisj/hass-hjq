"""Data update coordinator for 移动爱家 cameras."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .hjqapi import (
    HJQApi,
    HJQApiError,
    HJQAuthError,
    _BASE_URL_KEYS,
    _JWT_KEYS,
    _MAC_KEYS,
    _NAME_KEYS,
)

_LOGGER = logging.getLogger(__name__)


class HassHjqCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Periodically refresh camera list and keep auth alive."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: HJQApi,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=5),
        )
        self.entry = entry
        self.api = api

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch the camera list keyed by mac_id."""
        try:
            await self.api.ensure_auth()
            cameras = await self.api.get_camera_info()
        except HJQAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HJQApiError as err:
            raise UpdateFailed(str(err)) from err

        result: dict[str, dict[str, Any]] = {}
        for camera in cameras:
            mac_id = HJQApi._nested_str(camera, _MAC_KEYS)
            if not mac_id:
                continue
            camera["mac_id"] = mac_id
            camera["mac_name"] = HJQApi._nested_str(camera, _NAME_KEYS) or mac_id
            camera["baseUrl"] = HJQApi._nested_str(camera, _BASE_URL_KEYS)
            camera["jwtoken"] = HJQApi._nested_str(camera, _JWT_KEYS)
            if not camera["baseUrl"] or not camera["jwtoken"]:
                _LOGGER.warning(
                    "Camera %s (%s) missing live fields; keys=%s has_baseUrl=%s has_jwtoken=%s",
                    camera["mac_name"],
                    mac_id,
                    sorted(camera.keys()),
                    bool(camera["baseUrl"]),
                    bool(camera["jwtoken"]),
                )
            result[str(mac_id)] = camera
        if not result:
            _LOGGER.warning("No cameras returned for this account")
        return result

    def camera_data(self, mac_id: str) -> dict[str, Any] | None:
        """Return latest metadata for a camera."""
        if not self.data:
            return None
        return self.data.get(mac_id)
