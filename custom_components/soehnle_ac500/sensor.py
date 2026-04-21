"""Sensor platform for the Soehnle AC500."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AC500Coordinator
from .entity import AC500Entity


@dataclass(frozen=True, slots=True)
class AC500SensorDescription:
    """Description of an AC500 sensor."""

    key: str
    name: str
    value_fn: Callable
    icon: str | None = None
    native_unit_of_measurement: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    entity_category: EntityCategory | None = None
    suggested_display_precision: int | None = None


SENSORS: tuple[AC500SensorDescription, ...] = (
    AC500SensorDescription(
        key="pm25",
        name="PM2.5",
        value_fn=lambda state: state.status.pm25_ug_m3,
        native_unit_of_measurement="µg/m³",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="temperature",
        name="Temperature",
        value_fn=lambda state: state.status.temperature_c,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="filter",
        name="Filter life",
        value_fn=lambda state: state.status.filter_percent,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:air-filter",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    AC500SensorDescription(
        key="rssi",
        name="RSSI",
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

    def __init__(self, coordinator: AC500Coordinator, description: AC500SensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_entity_category = description.entity_category
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_suggested_display_precision = description.suggested_display_precision

    @property
    def native_value(self):
        """Return the sensor value."""
        state = self.coordinator.data
        if self.entity_description.key != "rssi" and state.status is None:
            return None
        return self.entity_description.value_fn(state)
