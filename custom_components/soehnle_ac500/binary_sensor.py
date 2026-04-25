"""Binary sensor entities for Soehnle AC500."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .coordinator import AC500Coordinator
from .entity import AC500Entity


async def async_setup_entry(entry_hass, entry: Any, async_add_entities) -> None:
    """Set up binary sensors."""
    async_add_entities([AC500ConnectedBinarySensor(entry.runtime_data)])


class AC500ConnectedBinarySensor(AC500Entity, BinarySensorEntity):
    """BLE connection state for one AC500."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AC500Coordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "connected")

    @property
    def is_on(self) -> bool:
        """Return true while a BLE session is currently connected."""
        return self.coordinator.device.connected
