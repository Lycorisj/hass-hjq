"""Diagnostics for 移动爱家."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .device import CameraWorker

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, "jwtoken", "jwToken", "token", "HJQToken"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    stored = hass.data[DOMAIN][entry.entry_id]
    workers: dict[str, CameraWorker] = stored.get("workers") or {}
    coordinator = stored["coordinator"]
    cameras = []
    for mac_id, worker in workers.items():
        cameras.append(
            {
                "mac_id": mac_id,
                "name": worker.name,
                "has_base_url": bool(worker.camera.get("baseUrl")),
                "has_live_url": bool(worker._cloud_url),  # noqa: SLF001
                "transcode": worker.transcode,
                "transcoder_running": worker.transcoder.running,
                "recording": worker.recording,
                "model": worker.dev_info.get("mac_model"),
            }
        )
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "coordinator_success": coordinator.last_update_success,
        "camera_count": len(cameras),
        "cameras": cameras,
    }
