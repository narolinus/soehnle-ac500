"""Select entities for Soehnle AC500."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .client import AC500CommunicationError
from .coordinator import AC500Coordinator
from .entity import AC500Entity
from .protocol import FAN_LABELS, TIMER_LABELS, AC500Status


@dataclass(frozen=True, slots=True)
class AC500SelectDescription:
    """Describe an AC500 select."""

    key: str
    translation_key: str
    icon: str
    options: list[str]
    value_fn: Callable[[AC500Status], str | None]
    set_fn: Callable[[AC500Coordinator, str], Awaitable[None]]


SELECTS = (
    AC500SelectDescription(
        key="fan_mode",
        translation_key="fan_mode",
        icon="mdi:fan",
        options=list(FAN_LABELS.values()),
        value_fn=lambda status: status.fan_label,
        set_fn=lambda coordinator, option: coordinator.async_set_fan_mode(option),
    ),
    AC500SelectDescription(
        key="timer",
        translation_key="timer",
        icon="mdi:timer-outline",
        options=list(TIMER_LABELS.values()),
        value_fn=lambda status: status.timer_label,
        set_fn=lambda coordinator, option: coordinator.async_set_timer(option),
    ),
)


async def async_setup_entry(entry_hass, entry: Any, async_add_entities) -> None:
    """Set up selects."""
    coordinator = entry.runtime_data
    async_add_entities(AC500Select(coordinator, description) for description in SELECTS)


class AC500Select(AC500Entity, SelectEntity):
    """Soehnle AC500 select."""

    entity_description: AC500SelectDescription

    def __init__(
        self,
        coordinator: AC500Coordinator,
        description: AC500SelectDescription,
    ) -> None:
        """Initialize select."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_options = description.options

    @property
    def current_option(self) -> str | None:
        """Return the selected option."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        """Select an option."""
        try:
            await self.entity_description.set_fn(self.coordinator, option)
        except AC500CommunicationError as err:
            raise HomeAssistantError(str(err)) from err
