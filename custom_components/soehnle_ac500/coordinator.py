"""Coordinator for Soehnle AC500 entities."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

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
            _LOGGER.debug("Could not update %s: %s", self.name, err)
            return self.data

    @callback
    def _async_device_updated(self) -> None:
        """Push BLE notification updates into HA entities."""
        self.async_set_updated_data(self.device.last_status)

    async def async_pair(self) -> None:
        """Run the AC500 pair action."""
        _LOGGER.warning("AC500 pair requested for %s", self.device.address)
        self._raise_if_busy()
        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Soehnle AC500 Pairing",
                    "message": (
                        f"{self.name}: Die Pairing-Aktion sendet jetzt fuer "
                        "kurze Zeit wiederholt Pairing-Anfragen. Bitte die "
                        "Bluetooth-Taste am Luftreiniger druecken."
                    ),
                    "notification_id": f"soehnle_ac500_pair_{self.device.address}",
                },
                blocking=False,
            )
        )
        status = await self.device.async_pair()
        if status is not None:
            self.async_set_updated_data(status)
        else:
            self.async_update_listeners()

    async def async_reconnect(self) -> None:
        """Run the reconnect action."""
        _LOGGER.warning("AC500 reconnect requested for %s", self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_reconnect())

    async def async_reset_bluetooth_cache(self) -> None:
        """Reset the cached BlueZ device object."""
        _LOGGER.warning("AC500 bluetooth cache reset requested for %s", self.device.address)
        self._raise_if_busy()
        await self.device.async_reset_bluetooth_cache()
        self.async_update_listeners()

    async def async_refresh(self) -> None:
        """Run an explicit refresh action."""
        _LOGGER.warning("AC500 refresh requested for %s", self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_refresh())

    async def async_set_power(self, enabled: bool) -> None:
        """Set power."""
        _LOGGER.warning("AC500 power=%s requested for %s", enabled, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_power(enabled))

    async def async_set_uv(self, enabled: bool) -> None:
        """Set UV-C."""
        _LOGGER.warning("AC500 uv=%s requested for %s", enabled, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_uv(enabled))

    async def async_set_auto(self, enabled: bool) -> None:
        """Set automatic mode."""
        _LOGGER.warning("AC500 auto=%s requested for %s", enabled, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_auto(enabled))

    async def async_set_night(self, enabled: bool) -> None:
        """Set night mode."""
        _LOGGER.warning("AC500 night=%s requested for %s", enabled, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_night(enabled))

    async def async_set_buzzer(self, enabled: bool) -> None:
        """Set buzzer."""
        _LOGGER.warning("AC500 buzzer=%s requested for %s", enabled, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_buzzer(enabled))

    async def async_set_fan_mode(self, mode: str) -> None:
        """Set fan mode."""
        _LOGGER.warning("AC500 fan_mode=%s requested for %s", mode, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_fan_mode(mode))

    async def async_set_timer(self, option: str) -> None:
        """Set timer."""
        _LOGGER.warning("AC500 timer=%s requested for %s", option, self.device.address)
        self._raise_if_busy()
        self.async_set_updated_data(await self.device.async_set_timer(option))

    async def async_shutdown(self) -> None:
        """Unload coordinator resources."""
        await self.device.async_shutdown()

    def _raise_if_busy(self) -> None:
        """Reject overlapping service calls instead of queuing them."""
        if self.device.busy:
            raise AC500CommunicationError(
                f"{self.name} is already running a Bluetooth operation"
            )
