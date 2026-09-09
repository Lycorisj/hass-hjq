"""Recording switch for 移动爱家 cameras."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HassHjqCoordinator
from .device import CameraWorker


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up recording switches."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator: HassHjqCoordinator = stored["coordinator"]
    workers: dict[str, CameraWorker] = stored["workers"]
    async_add_entities(
        HeJiaQinRecordSwitch(coordinator, worker) for worker in workers.values()
    )


class HeJiaQinRecordSwitch(CoordinatorEntity[HassHjqCoordinator], SwitchEntity):
    """Start / stop rolling local MP4 recording."""

    _attr_has_entity_name = True
    _attr_translation_key = "recording"
    _attr_icon = "mdi:record-rec"

    def __init__(
        self, coordinator: HassHjqCoordinator, worker: CameraWorker
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.worker = worker
        self._attr_unique_id = f"{worker.mac_id}_recording"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, worker.mac_id)},
            name=worker.name,
            manufacturer=MANUFACTURER,
        )

    @property
    def is_on(self) -> bool:
        """Return true if recording is active."""
        return self.worker.recording

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start recording."""
        try:
            await self.worker.async_start_recording()
        except Exception as err:
            raise HomeAssistantError(f"Unable to start recording: {err}") from err
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop recording."""
        await self.worker.async_stop_recording()
        self.async_write_ha_state()
