"""Select platform for the Soehnle AC500."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, TIMER_OPTIONS
from .coordinator import AC500Coordinator
from .entity import AC500Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AC500 selects."""
    coordinator: AC500Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AC500TimerSelect(coordinator)])


class AC500TimerSelect(AC500Entity, SelectEntity):
    """Expose the timer as a select entity."""

    _attr_translation_key = "timer"
    _attr_icon = "mdi:timer-outline"
    _attr_options = TIMER_OPTIONS

    def __init__(self, coordinator: AC500Coordinator) -> None:
        """Initialize the timer select."""
        super().__init__(coordinator, "timer")

    @property
    def current_option(self) -> str | None:
        """Return the active timer option."""
        status = self.coordinator.data.status
        return None if status is None else status.timer_label

    async def async_select_option(self, option: str) -> None:
        """Select a timer option."""
        await self.coordinator.async_set_timer(option)
