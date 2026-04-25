"""Text sensor entities for Soehnle AC500."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.text_sensor import TextSensorEntity
from homeassistant.const import EntityCategory

from .coordinator import AC500Coordinator
from .entity import AC500Entity


@dataclass(frozen=True, slots=True)
class AC500TextSensorDescription:
    """Describe an AC500 text sensor."""

    key: str
    translation_key: str
    icon: str
    value_fn: Callable[[AC500Coordinator], str | None]


TEXT_SENSORS = (
    AC500TextSensorDescription(
        key="state",
        translation_key="state",
        icon="mdi:information-outline",
        value_fn=lambda coordinator: coordinator.device.state,
    ),
    AC500TextSensorDescription(
        key="last_frame",
        translation_key="last_frame",
        icon="mdi:code-braces",
        value_fn=lambda coordinator: coordinator.device.last_frame_hex,
    ),
    AC500TextSensorDescription(
        key="last_ack",
        translation_key="last_ack",
        icon="mdi:code-brackets",
        value_fn=lambda coordinator: coordinator.device.last_ack_hex,
    ),
)


async def async_setup_entry(entry_hass, entry: Any, async_add_entities) -> None:
    """Set up text sensors."""
    coordinator = entry.runtime_data
    async_add_entities(AC500TextSensor(coordinator, description) for description in TEXT_SENSORS)


class AC500TextSensor(AC500Entity, TextSensorEntity):
    """Soehnle AC500 text sensor."""

    entity_description: AC500TextSensorDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: AC500Coordinator,
        description: AC500TextSensorDescription,
    ) -> None:
        """Initialize text sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon

    @property
    def native_value(self) -> str | None:
        """Return text value."""
        return self.entity_description.value_fn(self.coordinator)
