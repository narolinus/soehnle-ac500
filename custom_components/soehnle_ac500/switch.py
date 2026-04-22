"""Switch platform for the Soehnle AC500."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AC500Coordinator
from .entity import AC500Entity


@dataclass(frozen=True, slots=True)
class AC500SwitchDescription:
    """Description of an AC500 switch."""

    key: str
    name: str
    icon: str
    value_fn: Callable
    setter_name: str
    device_class: str | None = None
    entity_registry_enabled_default: bool = True
    entity_category: EntityCategory | None = None


SWITCHES: tuple[AC500SwitchDescription, ...] = (
    AC500SwitchDescription(
        key="uv",
        name="UV-C",
        icon="mdi:lightbulb-ultraviolet",
        value_fn=lambda status: status.uv_enabled,
        setter_name="async_set_uv",
    ),
    AC500SwitchDescription(
        key="auto",
        name="Auto mode",
        icon="mdi:brightness-auto",
        value_fn=lambda status: status.auto_enabled,
        setter_name="async_set_auto",
    ),
    AC500SwitchDescription(
        key="night",
        name="Night mode",
        icon="mdi:weather-night",
        value_fn=lambda status: status.night_enabled,
        setter_name="async_set_night",
    ),
    AC500SwitchDescription(
        key="buzzer",
        name="Buzzer",
        icon="mdi:volume-high",
        value_fn=lambda status: status.buzzer_enabled,
        setter_name="async_set_buzzer",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AC500 switches."""
    coordinator: AC500Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(AC500SwitchEntity(coordinator, description) for description in SWITCHES)


class AC500SwitchEntity(AC500Entity, SwitchEntity):
    """Representation of a switchable AC500 setting."""

    def __init__(self, coordinator: AC500Coordinator, description: AC500SwitchDescription) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category

    @property
    def is_on(self) -> bool | None:
        """Return the current switch state."""
        status = self.coordinator.data.status
        return None if status is None else self.entity_description.value_fn(status)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        del kwargs
        await getattr(self.coordinator, self.entity_description.setter_name)(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        del kwargs
        await getattr(self.coordinator, self.entity_description.setter_name)(False)
