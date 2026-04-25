"""Sensor entities for Soehnle AC500."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
)

from .coordinator import AC500Coordinator
from .entity import AC500Entity
from .protocol import AC500Status


@dataclass(frozen=True, kw_only=True)
class AC500SensorDescription(SensorEntityDescription):
    """Describe an AC500 sensor."""

    value_fn: Callable[[AC500Coordinator], int | float | str | None]


def _status_value(
    coordinator: AC500Coordinator,
    fn: Callable[[AC500Status], int | float | str | None],
) -> int | float | str | None:
    """Return a value from the current status."""
    if coordinator.data is None:
        return None
    return fn(coordinator.data)


SENSORS = (
    AC500SensorDescription(
        key="pm25",
        translation_key="pm25",
        icon="mdi:molecule",
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _status_value(
            coordinator,
            lambda status: status.pm25_ug_m3,
        ),
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="temperature",
        translation_key="temperature",
        icon=None,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _status_value(
            coordinator,
            lambda status: status.temperature_c,
        ),
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="filter",
        translation_key="filter",
        icon="mdi:air-filter",
        native_unit_of_measurement=PERCENTAGE,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _status_value(
            coordinator,
            lambda status: status.filter_percent,
        ),
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="rssi",
        translation_key="rssi",
        icon=None,
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: coordinator.device.rssi,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AC500SensorDescription(
        key="state",
        translation_key="state",
        icon="mdi:information-outline",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda coordinator: coordinator.device.state,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AC500SensorDescription(
        key="last_frame",
        translation_key="last_frame",
        icon="mdi:code-braces",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda coordinator: coordinator.device.last_frame_hex,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AC500SensorDescription(
        key="last_ack",
        translation_key="last_ack",
        icon="mdi:code-brackets",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda coordinator: coordinator.device.last_ack_hex,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AC500SensorDescription(
        key="last_error",
        translation_key="last_error",
        icon="mdi:alert-circle-outline",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=None,
        value_fn=lambda coordinator: coordinator.device.last_error,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(entry_hass, entry: Any, async_add_entities) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(AC500Sensor(coordinator, description) for description in SENSORS)


class AC500Sensor(AC500Entity, SensorEntity):
    """Soehnle AC500 sensor."""

    entity_description: AC500SensorDescription

    def __init__(
        self,
        coordinator: AC500Coordinator,
        description: AC500SensorDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_suggested_display_precision = description.suggested_display_precision
        self._attr_entity_category = description.entity_category

    @property
    def native_value(self) -> int | float | str | None:
        """Return native sensor value."""
        return self.entity_description.value_fn(self.coordinator)
