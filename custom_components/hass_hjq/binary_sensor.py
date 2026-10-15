"""Motion sensor fed by the local H.264 stream."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HassHjqCoordinator
from .device import CameraWorker


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up motion sensors."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: HassHjqCoordinator = stored["coordinator"]
    workers: dict[str, CameraWorker] = stored["workers"]
    async_add_entities(
        HeJiaQinMotionSensor(coordinator, worker) for worker in workers.values()
    )


class HeJiaQinMotionSensor(CoordinatorEntity[HassHjqCoordinator], BinarySensorEntity):
    """Motion detected on the transcoded stream."""

    _attr_has_entity_name = True
    _attr_translation_key = "motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self, coordinator: HassHjqCoordinator, worker: CameraWorker
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.worker = worker
        self._attr_unique_id = f"{worker.mac_id}_motion"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, worker.mac_id)},
            name=worker.name,
            manufacturer=MANUFACTURER,
        )

    @property
    def is_on(self) -> bool:
        """Return true while motion is being held."""
        return self.worker.motion_is_on

    async def async_added_to_hass(self) -> None:
        """Update when motion fires, and clear it after the hold expires."""
        await super().async_added_to_hass()
        self.async_on_remove(self.worker.listen(self.async_write_ha_state))
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._expire, timedelta(seconds=5)
            )
        )

    def _expire(self, _now) -> None:
        self.async_write_ha_state()
