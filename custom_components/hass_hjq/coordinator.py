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
from .hjqapi import HJQApi, HJQApiError, HJQAuthError

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
            mac_id = camera.get("mac_id") or camera.get("macId")
            if not mac_id:
                continue
            camera["mac_id"] = mac_id
            camera["mac_name"] = (
                camera.get("mac_name")
                or camera.get("macName")
                or camera.get("name")
                or mac_id
            )
            camera["baseUrl"] = camera.get("baseUrl") or camera.get("base_url") or ""
            camera["jwtoken"] = camera.get("jwtoken") or camera.get("jwToken") or ""
            result[str(mac_id)] = camera
        if not result:
            _LOGGER.warning("No cameras returned for this account")
        return result

    def camera_data(self, mac_id: str) -> dict[str, Any] | None:
        """Return latest metadata for a camera."""
        if not self.data:
            return None
        return self.data.get(mac_id)
