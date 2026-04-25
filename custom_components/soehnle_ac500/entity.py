"""Base entity for Soehnle AC500."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AC500Coordinator


class AC500Entity(CoordinatorEntity[AC500Coordinator]):
    """Base entity for one AC500 device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AC500Coordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._key = key
        address = coordinator.config_entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=coordinator.config_entry.data.get(CONF_NAME, coordinator.config_entry.title),
        )
