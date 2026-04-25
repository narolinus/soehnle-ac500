"""Button entities for Soehnle AC500."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .client import AC500CommunicationError
from .coordinator import AC500Coordinator
from .entity import AC500Entity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AC500ButtonDescription(ButtonEntityDescription):
    """Describe an AC500 button."""

    press_fn: Callable[[AC500Coordinator], Awaitable[None]]


BUTTONS = (
    AC500ButtonDescription(
        key="pair",
        translation_key="pair",
        icon="mdi:bluetooth-connect",
        press_fn=lambda coordinator: coordinator.async_pair(),
    ),
    AC500ButtonDescription(
        key="refresh",
        translation_key="refresh",
        icon="mdi:refresh",
        press_fn=lambda coordinator: coordinator.async_refresh(),
    ),
    AC500ButtonDescription(
        key="reconnect",
        translation_key="reconnect",
        icon="mdi:bluetooth-transfer",
        press_fn=lambda coordinator: coordinator.async_reconnect(),
    ),
)


async def async_setup_entry(entry_hass, entry: Any, async_add_entities) -> None:
    """Set up buttons."""
    coordinator = entry.runtime_data
    async_add_entities(AC500Button(coordinator, description) for description in BUTTONS)


class AC500Button(AC500Entity, ButtonEntity):
    """Soehnle AC500 button."""

    entity_description: AC500ButtonDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: AC500Coordinator,
        description: AC500ButtonDescription,
    ) -> None:
        """Initialize button."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        if description.key == "pair":
            self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        """Press the button."""
        _LOGGER.warning(
            "AC500 action requested: %s for %s",
            self.entity_description.key,
            self.coordinator.device.address,
        )
        try:
            await self.entity_description.press_fn(self.coordinator)
        except AC500CommunicationError as err:
            _LOGGER.exception(
                "AC500 action failed: %s for %s",
                self.entity_description.key,
                self.coordinator.device.address,
            )
            raise HomeAssistantError(str(err)) from err
