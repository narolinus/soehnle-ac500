"""Shared entity helpers for the Soehnle AC500 integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import AC500Coordinator


class AC500Entity(CoordinatorEntity[AC500Coordinator]):
    """Base entity for an AC500."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: AC500Coordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address.lower()}_{key}"

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return self.coordinator.data.available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device metadata."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self.coordinator.data.name,
        )
