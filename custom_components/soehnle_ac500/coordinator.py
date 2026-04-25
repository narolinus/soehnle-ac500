"""Coordinator for Soehnle AC500 entities."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AC500CommunicationError, AC500Device
from .const import CONF_ADDRESS, CONF_NAME, SCAN_INTERVAL
from .protocol import AC500Status

_LOGGER = logging.getLogger(__name__)


class AC500Coordinator(DataUpdateCoordinator[AC500Status | None]):
    """Coordinate all entities for one AC500."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.device = AC500Device(
            hass,
            entry.data[CONF_ADDRESS],
            entry.data.get(CONF_NAME, entry.title),
            self._async_device_updated,
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> AC500Status | None:
        """Poll the AC500 for a fresh live frame."""
        try:
            return await self.device.async_update()
        except AC500CommunicationError as err:
            raise UpdateFailed(str(err)) from err

    @callback
    def _async_device_updated(self) -> None:
        """Push BLE notification updates into HA entities."""
        self.async_set_updated_data(self.device.last_status)

    async def async_pair(self) -> None:
        """Run the AC500 pair action."""
        self.async_set_updated_data(await self.device.async_pair())

    async def async_reconnect(self) -> None:
        """Run the reconnect action."""
        self.async_set_updated_data(await self.device.async_reconnect())

    async def async_refresh(self) -> None:
        """Run an explicit refresh action."""
        await self.async_request_refresh()

    async def async_set_power(self, enabled: bool) -> None:
        """Set power."""
        self.async_set_updated_data(await self.device.async_set_power(enabled))

    async def async_set_uv(self, enabled: bool) -> None:
        """Set UV-C."""
        self.async_set_updated_data(await self.device.async_set_uv(enabled))

    async def async_set_auto(self, enabled: bool) -> None:
        """Set automatic mode."""
        self.async_set_updated_data(await self.device.async_set_auto(enabled))

    async def async_set_night(self, enabled: bool) -> None:
        """Set night mode."""
        self.async_set_updated_data(await self.device.async_set_night(enabled))

    async def async_set_buzzer(self, enabled: bool) -> None:
        """Set buzzer."""
        self.async_set_updated_data(await self.device.async_set_buzzer(enabled))

    async def async_set_fan_mode(self, mode: str) -> None:
        """Set fan mode."""
        self.async_set_updated_data(await self.device.async_set_fan_mode(mode))

    async def async_set_timer(self, option: str) -> None:
        """Set timer."""
        self.async_set_updated_data(await self.device.async_set_timer(option))

    async def async_shutdown(self) -> None:
        """Unload coordinator resources."""
        await self.device.async_shutdown()
