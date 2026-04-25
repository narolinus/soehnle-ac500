"""Soehnle Airclean Connect 500 integration."""

from __future__ import annotations

from typing import Any

from .const import PLATFORMS


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Soehnle AC500 from a config entry."""
    from .coordinator import AC500Coordinator

    coordinator = AC500Coordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime_data: Any = entry.runtime_data
        await runtime_data.async_shutdown()
    return unload_ok
