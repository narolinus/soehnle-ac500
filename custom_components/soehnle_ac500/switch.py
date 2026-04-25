"""Switch entities for Soehnle AC500."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .client import AC500CommunicationError
from .coordinator import AC500Coordinator
from .entity import AC500Entity
from .protocol import AC500Status


@dataclass(frozen=True, kw_only=True)
class AC500SwitchDescription(SwitchEntityDescription):
    """Describe an AC500 switch."""

    value_fn: Callable[[AC500Status], bool]
    set_fn: Callable[[AC500Coordinator, bool], Awaitable[None]]


SWITCHES = (
    AC500SwitchDescription(
        key="power",
        translation_key="power",
        icon="mdi:power",
        value_fn=lambda status: status.power_enabled,
        set_fn=lambda coordinator, enabled: coordinator.async_set_power(enabled),
    ),
    AC500SwitchDescription(
        key="uv",
        translation_key="uv",
        icon="mdi:lightbulb-cfl",
        value_fn=lambda status: status.uv_enabled,
        set_fn=lambda coordinator, enabled: coordinator.async_set_uv(enabled),
    ),
    AC500SwitchDescription(
        key="auto",
        translation_key="auto",
        icon="mdi:fan-auto",
        value_fn=lambda status: status.auto_enabled,
        set_fn=lambda coordinator, enabled: coordinator.async_set_auto(enabled),
    ),
    AC500SwitchDescription(
        key="night",
        translation_key="night",
        icon="mdi:weather-night",
        value_fn=lambda status: status.night_enabled,
        set_fn=lambda coordinator, enabled: coordinator.async_set_night(enabled),
    ),
    AC500SwitchDescription(
        key="buzzer",
        translation_key="buzzer",
        icon="mdi:volume-high",
        value_fn=lambda status: status.buzzer_enabled,
        set_fn=lambda coordinator, enabled: coordinator.async_set_buzzer(enabled),
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(entry_hass, entry: Any, async_add_entities) -> None:
    """Set up switches."""
    coordinator = entry.runtime_data
    async_add_entities(AC500Switch(coordinator, description) for description in SWITCHES)


class AC500Switch(AC500Entity, SwitchEntity):
    """Soehnle AC500 switch."""

    entity_description: AC500SwitchDescription

    def __init__(
        self,
        coordinator: AC500Coordinator,
        description: AC500SwitchDescription,
    ) -> None:
        """Initialize switch."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category

    @property
    def is_on(self) -> bool | None:
        """Return switch state."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Set switch state."""
        try:
            await self.entity_description.set_fn(self.coordinator, enabled)
        except AC500CommunicationError as err:
            raise HomeAssistantError(str(err)) from err
