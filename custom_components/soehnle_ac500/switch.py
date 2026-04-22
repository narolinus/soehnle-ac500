"""Switch platform for the Soehnle AC500."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AC500Coordinator
from .entity import AC500Entity
from .protocol import AC500Status


@dataclass(frozen=True, kw_only=True)
class AC500SwitchDescription(SwitchEntityDescription):
    """Description of an AC500 switch."""

    value_fn: Callable[[AC500Status], bool] = lambda status: False
    setter_name: str = ""


SWITCHES: tuple[AC500SwitchDescription, ...] = (
    AC500SwitchDescription(
        key="uv",
        translation_key="uv",
        icon="mdi:lightbulb-ultraviolet",
        value_fn=lambda status: status.uv_enabled,
        setter_name="async_set_uv",
    ),
    AC500SwitchDescription(
        key="auto",
        translation_key="auto",
        icon="mdi:brightness-auto",
        value_fn=lambda status: status.auto_enabled,
        setter_name="async_set_auto",
    ),
    AC500SwitchDescription(
        key="night",
        translation_key="night",
        icon="mdi:weather-night",
        value_fn=lambda status: status.night_enabled,
        setter_name="async_set_night",
    ),
    AC500SwitchDescription(
        key="buzzer",
        translation_key="buzzer",
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

    entity_description: AC500SwitchDescription

    def __init__(self, coordinator: AC500Coordinator, description: AC500SwitchDescription) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

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
