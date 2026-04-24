"""Button platform for the Soehnle AC500."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import AC500Coordinator
from .entity import AC500Entity


@dataclass(frozen=True, kw_only=True)
class AC500ButtonDescription(ButtonEntityDescription):
    """Description of an AC500 button."""

    press_fn: Callable[[AC500Coordinator], Coroutine[Any, Any, None]]


BUTTONS: tuple[AC500ButtonDescription, ...] = (
    AC500ButtonDescription(
        key="pair",
        translation_key="pair",
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda coordinator: coordinator.async_run_pairing_handshake(),
    ),
    AC500ButtonDescription(
        key="refresh",
        translation_key="refresh",
        icon="mdi:refresh",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda coordinator: coordinator.async_request_refresh(),
    ),
    AC500ButtonDescription(
        key="reconnect",
        translation_key="reconnect",
        icon="mdi:bluetooth-transfer",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda coordinator: coordinator.async_reconnect(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AC500 buttons."""
    coordinator: AC500Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(AC500ButtonEntity(coordinator, description) for description in BUTTONS)


class AC500ButtonEntity(AC500Entity, ButtonEntity):
    """Representation of an AC500 action button."""

    entity_description: AC500ButtonDescription

    def __init__(self, coordinator: AC500Coordinator, description: AC500ButtonDescription) -> None:
        """Initialize the button."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Buttons must stay usable even if the live status path is currently down."""
        return True

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.entity_description.press_fn(self.coordinator)
