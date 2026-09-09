"""The 移动爱家（原和家亲） integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, PLATFORMS
from .coordinator import HassHjqCoordinator
from .device import CameraWorker
from .hjqapi import HJQApi, HJQApiError, HJQAuthError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (YAML is not used)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up 移动爱家 from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    if not entry.unique_id:
        hass.config_entries.async_update_entry(
            entry, unique_id=entry.data[CONF_USERNAME]
        )
    session = async_get_clientsession(hass)
    api = HJQApi(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], session)

    coordinator = HassHjqCoordinator(hass, entry, api)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:
        raise ConfigEntryNotReady(str(err)) from err

    if coordinator.last_update_success is False:
        raise ConfigEntryNotReady("Unable to fetch camera list")

    workers: dict[str, CameraWorker] = {}
    for mac_id, camera in (coordinator.data or {}).items():
        try:
            dev_info = await api.get_device_info(
                camera.get("baseUrl") or "",
                camera.get("jwtoken") or "",
                mac_id,
            )
        except (HJQApiError, HJQAuthError) as err:
            _LOGGER.debug("Device info failed for %s: %s", mac_id, err)
            dev_info = {}
        workers[mac_id] = CameraWorker(hass, entry, coordinator, camera, dev_info)

    if not workers:
        _LOGGER.warning(
            "Account logged in but no cameras were returned. "
            "Some models use a different device-list API."
        )

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "workers": workers,
        "api": api,
    }

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    stored = hass.data[DOMAIN].get(entry.entry_id) or {}
    workers: dict[str, CameraWorker] = stored.get("workers") or {}
    for worker in workers.values():
        await worker.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
