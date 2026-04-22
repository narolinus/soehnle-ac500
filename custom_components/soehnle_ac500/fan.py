"""Fan platform for the Soehnle AC500."""

from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .const import DOMAIN, FAN_LEVELS
from .coordinator import AC500Coordinator
from .entity import AC500Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the AC500 fan entity."""
    coordinator: AC500Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AC500FanEntity(coordinator)])


class AC500FanEntity(AC500Entity, FanEntity):
    """Expose the purifier as a fan."""

    _attr_name = None
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_OFF | FanEntityFeature.TURN_ON
    )
    _attr_icon = "mdi:air-purifier"

    def __init__(self, coordinator: AC500Coordinator) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator, "fan")

    @property
    def is_on(self) -> bool | None:
        """Return whether the purifier is on."""
        status = self.coordinator.data.status
        return None if status is None else status.power_enabled

    @property
    def percentage(self) -> int | None:
        """Return the current percentage."""
        status = self.coordinator.data.status
        if status is None or not status.power_enabled:
            return 0
        return ordered_list_item_to_percentage(FAN_LEVELS, status.fan_label)

    @property
    def speed_count(self) -> int:
        """Return the number of manual speeds."""
        return len(FAN_LEVELS)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs,
    ) -> None:
        """Turn on the purifier."""
        del preset_mode, kwargs
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the purifier off."""
        del kwargs
        await self.coordinator.async_set_power(False)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the manual fan percentage."""
        if percentage <= 0:
            await self.coordinator.async_set_power(False)
            return

        level = percentage_to_ordered_list_item(FAN_LEVELS, percentage)
        await self.coordinator.async_set_fan_level(level)
