"""Camera platform for 移动爱家 / 和家亲."""

from __future__ import annotations

import logging
from typing import Any

from haffmpeg.tools import IMAGE_JPEG
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.components.stream import Stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HassHjqCoordinator
from .device import CameraWorker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up cameras from a config entry."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: HassHjqCoordinator = stored["coordinator"]
    workers: dict[str, CameraWorker] = stored["workers"]
    entities = [
        HeJiaQinCamera(coordinator, worker) for worker in workers.values()
    ]
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "refresh_stream",
        None,
        "async_refresh_stream",
    )


class HeJiaQinCamera(CoordinatorEntity[HassHjqCoordinator], Camera):
    """HeJiaQin cloud camera with optional H.264 transcode."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_is_streaming = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, coordinator: HassHjqCoordinator, worker: CameraWorker
    ) -> None:
        """Initialize the camera."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.worker = worker
        self._attr_unique_id = worker.mac_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, worker.mac_id)},
            name=worker.name,
            manufacturer=MANUFACTURER,
            model=worker.dev_info.get("mac_model")
            or worker.camera.get("mac_model")
            or worker.camera.get("model")
            or "Camera",
            sw_version=worker.dev_info.get("firmware_model")
            or worker.dev_info.get("firmware_version"),
        )

    @property
    def use_stream_for_stills(self) -> bool:
        """Generate stills from the live stream instead of a snapshot URL."""
        return True

    @property
    def is_recording(self) -> bool:
        """Return true if the camera is recording locally."""
        return self.worker.recording

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        info = self.worker.dev_info
        return {
            "ip_address": info.get("ip_address") or info.get("ipAddress"),
            "mac_address": info.get("mac_addr") or info.get("macAddr"),
            "transcoding_h264": self.worker.transcode,
            "h264_ready": self.worker.transcoder.running,
            "recording": self.worker.recording,
        }

    async def async_added_to_hass(self) -> None:
        """Start keep-alive when the entity is added."""
        await super().async_added_to_hass()
        await self.worker.async_start()

    async def async_will_remove_from_hass(self) -> None:
        """Stop background work when the entity is removed."""
        await self.worker.async_stop()
        if self.stream:
            await self.stream.stop()
            self.stream = None
        await super().async_will_remove_from_hass()

    async def stream_source(self) -> str | None:
        """Return the source URL used by HA stream and HomeKit ffmpeg."""
        return await self.worker.async_stream_source()

    async def async_create_stream(self) -> Stream | None:
        """Create (or reuse) the Home Assistant Stream object."""
        try:
            await self.worker.async_cloud_url()
            stream = await super().async_create_stream()
            if stream is not None and hasattr(stream, "dynamic_stream_settings"):
                stream.dynamic_stream_settings.preload_stream = True
            return stream
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error opening stream for %s", self.worker.name)
            return self.stream

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still JPEG from the live source."""
        source = await self.worker.async_stream_source()
        if not source:
            return None
        try:
            return await async_get_image(
                self.hass,
                source,
                extra_cmd="-ss 0",
                output_format=IMAGE_JPEG,
                width=width,
                height=height,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Still image failed for %s", self.worker.name, exc_info=True)
            return None

    async def async_refresh_stream(self) -> None:
        """Service: force a new live address and restart transcoding."""
        await self.worker.async_stream_source(force=True)
        if self.stream and self.worker.transcode:
            self.stream.update_source(self.worker.transcoder.playlist_path)
        elif self.stream:
            cloud = await self.worker.async_cloud_url()
            if cloud:
                self.stream.update_source(cloud)
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated camera metadata."""
        latest = self.coordinator.camera_data(self.worker.mac_id)
        self._attr_available = latest is not None
        super()._handle_coordinator_update()
