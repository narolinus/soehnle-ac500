"""Sensor platform for the Soehnle AC500."""

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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AC500Coordinator, AC500RuntimeState
from .entity import AC500Entity


@dataclass(frozen=True, kw_only=True)
class AC500SensorDescription(SensorEntityDescription):
    """Description of an AC500 sensor."""

    value_fn: Callable[[AC500RuntimeState], Any] = lambda state: None


SENSORS: tuple[AC500SensorDescription, ...] = (
    AC500SensorDescription(
        key="pm25",
        translation_key="pm25",
        value_fn=lambda state: state.status.pm25_ug_m3 if state.status else None,
        native_unit_of_measurement="µg/m³",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="temperature",
        translation_key="temperature",
        value_fn=lambda state: state.status.temperature_c if state.status else None,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="filter",
        translation_key="filter_life",
        value_fn=lambda state: state.status.filter_percent if state.status else None,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:air-filter",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="rssi",
        translation_key="rssi",
        value_fn=lambda state: state.rssi,
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AC500 sensors."""
    coordinator: AC500Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(AC500SensorEntity(coordinator, description) for description in SENSORS)


class AC500SensorEntity(AC500Entity, SensorEntity):
    """Representation of an AC500 sensor."""

    entity_description: AC500SensorDescription

    def __init__(self, coordinator: AC500Coordinator, description: AC500SensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)
