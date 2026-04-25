"""Soehnle Airclean Connect 500 integration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS

if TYPE_CHECKING:
    from .coordinator import AC500Coordinator

    AC500ConfigEntry: TypeAlias = ConfigEntry[AC500Coordinator]
else:
    AC500ConfigEntry: TypeAlias = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: AC500ConfigEntry) -> bool:
    """Set up Soehnle AC500 from a config entry."""
    from .coordinator import AC500Coordinator

    coordinator = AC500Coordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass,
        coordinator.async_request_refresh(),
        "soehnle_ac500_initial_refresh",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AC500ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
