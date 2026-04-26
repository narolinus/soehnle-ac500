"""Soehnle Airclean Connect 500 integration."""

from __future__ import annotations

import logging
from typing import Any

from .const import PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Soehnle AC500 from a config entry."""
    from .coordinator import AC500Coordinator

    _LOGGER.warning(
        "soehnle_ac500 setup entry title=%s entry_id=%s data=%s",
        entry.title,
        entry.entry_id,
        dict(entry.data),
    )

    coordinator = AC500Coordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    _LOGGER.warning(
        "soehnle_ac500 setup entry complete title=%s entry_id=%s",
        entry.title,
        entry.entry_id,
    )
    return True


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload a config entry."""
    _LOGGER.warning(
        "soehnle_ac500 unload entry title=%s entry_id=%s",
        entry.title,
        entry.entry_id,
    )
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime_data: Any = entry.runtime_data
        await runtime_data.async_shutdown()
    return unload_ok
