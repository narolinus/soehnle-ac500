"""Coordinator for the Soehnle AC500 integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ble import AC500BleSession
from .const import (
    DEFAULT_NAME,
    DOMAIN,
    FAN_LEVELS,
    SESSION_CONTROL,
    SESSION_IDLE,
    SESSION_PAIRING,
    SESSION_STATUS_POLL,
    STATUS_WAIT_TIMEOUT,
    TIMER_OPTIONS,
    UPDATE_INTERVAL,
)
from .protocol import (
    AUTO_COMMANDS,
    BUZZER_COMMANDS,
    FAN_COMMANDS,
    NIGHT_COMMANDS,
    POWER_COMMANDS,
    TIMER_COMMANDS,
    UV_COMMANDS,
    AC500Status,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class AC500RuntimeState:
    """Immutable runtime snapshot exposed to entities."""

    address: str
    name: str
    status: AC500Status | None
    available: bool
    connectable: bool
    rssi: int | None
    last_seen: datetime | None
    last_error: str | None
    session_state: str
    paired: bool
    control_mode_active: bool


class AC500Coordinator(DataUpdateCoordinator[AC500RuntimeState]):
    """Coordinate on-demand status and control sessions."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self._name = entry.data.get(CONF_NAME, DEFAULT_NAME)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.address}",
            update_interval=UPDATE_INTERVAL,
        )

        self._operation_lock = asyncio.Lock()
        self._status: AC500Status | None = None
        self._status_ok = False
        self._connectable = False
        self._rssi: int | None = None
        self._last_seen: datetime | None = None
        self._last_error: str | None = None
        self._session_state = SESSION_IDLE
        self._paired = False

        self._refresh_discovery_data()
        self.async_set_updated_data(self._build_state())

    async def async_start(self) -> None:
        """Prime the first status update without failing setup."""
        await self.async_refresh()

    async def async_stop(self) -> None:
        """Stop the coordinator."""
        return None

    async def _async_update_data(self) -> AC500RuntimeState:
        """Periodic refresh."""
        return await self._async_poll_status(raise_on_failure=False)

    async def async_refresh_status(self) -> None:
        """Refresh live status and raise on failure."""
        await self._async_poll_status(raise_on_failure=True)

    async def async_set_power(self, enabled: bool) -> None:
        """Set power state."""
        await self._async_execute_command(
            POWER_COMMANDS["on" if enabled else "off"],
            lambda status: status.power_enabled is enabled,
        )

    async def async_set_uv(self, enabled: bool) -> None:
        """Set UV-C state."""
        await self._async_execute_command(
            UV_COMMANDS["on" if enabled else "off"],
            lambda status: status.uv_enabled is enabled,
        )

    async def async_set_auto(self, enabled: bool) -> None:
        """Set auto mode."""
        await self._async_execute_command(
            AUTO_COMMANDS["on" if enabled else "off"],
            lambda status: status.auto_enabled is enabled,
        )

    async def async_set_night(self, enabled: bool) -> None:
        """Set night mode."""
        await self._async_execute_command(
            NIGHT_COMMANDS["on" if enabled else "off"],
            lambda status: status.night_enabled is enabled,
        )

    async def async_set_buzzer(self, enabled: bool) -> None:
        """Set buzzer mode."""
        await self._async_execute_command(
            BUZZER_COMMANDS["on" if enabled else "off"],
            lambda status: status.buzzer_enabled is enabled,
        )

    async def async_set_timer(self, option: str) -> None:
        """Set timer option."""
        if option not in TIMER_OPTIONS:
            raise HomeAssistantError(f"Unsupported timer option: {option}")

        await self._async_execute_command(
            TIMER_COMMANDS[option],
            lambda status, expected=option: status.timer_label == expected,
        )

    async def async_set_fan_level(self, level: str) -> None:
        """Set manual fan level."""
        if level not in FAN_LEVELS:
            raise HomeAssistantError(f"Unsupported fan level: {level}")

        async with self._operation_lock:
            self._session_state = SESSION_CONTROL
            self._last_error = None
            self._publish_state()

            error: HomeAssistantError | None = None
            try:
                async with self._async_open_session() as session:
                    baseline = await session.async_enter_control_mode()
                    current = baseline

                    if current is not None and current.auto_enabled:
                        await session.async_write_frame(*AUTO_COMMANDS["off"])
                        current = await session.async_wait_for_status(
                            lambda status: not status.auto_enabled,
                            timeout=STATUS_WAIT_TIMEOUT,
                        )
                        if current is None or current.auto_enabled:
                            raise HomeAssistantError(
                                "The AC500 did not leave auto mode before the manual fan change."
                            )

                    reference = current.raw_frame_hex if current is not None else None
                    await session.async_write_frame(*FAN_COMMANDS[level])
                    status = await session.async_wait_for_status(
                        lambda result, expected=level: result.fan_label == expected
                        and not result.auto_enabled,
                        timeout=STATUS_WAIT_TIMEOUT,
                    )
                    self._apply_command_result(
                        status,
                        lambda result, expected=level: result.fan_label == expected
                        and not result.auto_enabled,
                        reference,
                    )
            except HomeAssistantError as err:
                error = err
                self._last_error = str(err)
                raise
            finally:
                self._session_state = SESSION_IDLE
                self._publish_state()
                if error is None:
                    self._last_error = None

    async def async_run_pairing_handshake(self) -> None:
        """Run BLE link pairing and the AC500 EF03 handshake."""
        async with self._operation_lock:
            self._session_state = SESSION_PAIRING
            self._last_error = None
            self._publish_state()

            error: HomeAssistantError | None = None
            try:
                async with self._async_open_session() as session:
                    await session.async_run_pairing_handshake()
                    status = await session.async_initialize_status_channel()
                    if status is None:
                        raise HomeAssistantError(
                            "Pairing completed, but no live status frame was received afterwards."
                        )

                    self._status = status
                    self._status_ok = True
                    self._paired = True
            except HomeAssistantError as err:
                error = err
                self._status_ok = False
                self._last_error = str(err)
                raise
            finally:
                self._session_state = SESSION_IDLE
                self._publish_state()
                if error is None:
                    self._last_error = None

    async def _async_poll_status(self, *, raise_on_failure: bool) -> AC500RuntimeState:
        """Run one status-only poll session."""
        async with self._operation_lock:
            self._session_state = SESSION_STATUS_POLL
            self._last_error = None
            self._publish_state()

            error: HomeAssistantError | None = None
            try:
                async with self._async_open_session() as session:
                    status = await session.async_initialize_status_channel()
                    if status is None:
                        raise HomeAssistantError("No live status frame was received from the AC500.")

                    self._status = status
                    self._status_ok = True
            except HomeAssistantError as err:
                error = err
                self._status_ok = False
                self._last_error = str(err)
                if raise_on_failure:
                    raise
            finally:
                self._session_state = SESSION_IDLE
                self._publish_state()
                if error is None:
                    self._last_error = None

        return self.data

    async def _async_execute_command(self, frame: tuple[int, int, int], predicate) -> None:
        """Open control mode, send one command and validate the new status."""
        async with self._operation_lock:
            self._session_state = SESSION_CONTROL
            self._last_error = None
            self._publish_state()

            error: HomeAssistantError | None = None
            try:
                async with self._async_open_session() as session:
                    baseline = await session.async_enter_control_mode()
                    reference = baseline.raw_frame_hex if baseline is not None else None
                    await session.async_write_frame(*frame)
                    status = await session.async_wait_for_status(
                        predicate,
                        timeout=STATUS_WAIT_TIMEOUT,
                    )
                    self._apply_command_result(status, predicate, reference)
            except HomeAssistantError as err:
                error = err
                self._last_error = str(err)
                raise
            finally:
                self._session_state = SESSION_IDLE
                self._publish_state()
                if error is None:
                    self._last_error = None

    def _apply_command_result(
        self,
        status: AC500Status | None,
        predicate,
        reference_frame: str | None,
    ) -> None:
        """Validate and store the status returned by a command."""
        if status is None:
            raise HomeAssistantError("No updated status frame was received after the command.")
        if not predicate(status):
            raise HomeAssistantError(
                "The AC500 did not confirm the requested state change. If control is not authorized yet, use the Pair action and press the Bluetooth button on the purifier."
            )
        if reference_frame is not None and status.raw_frame_hex == reference_frame:
            raise HomeAssistantError(
                "The AC500 kept reporting the same state after the write. If control is not authorized yet, use the Pair action and press the Bluetooth button on the purifier."
            )

        self._status = status
        self._status_ok = True
        self._paired = True

    def _async_open_session(self) -> AC500BleSession:
        """Create one transient BLE session."""
        return AC500BleSession(
            self.address,
            self._name,
            self._async_resolve_ble_device,
        )

    def _async_resolve_ble_device(self):
        """Resolve the freshest connectable BLE device."""
        return bluetooth.async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )

    def _refresh_discovery_data(self) -> None:
        """Refresh cached discovery data from Home Assistant Bluetooth."""
        self._connectable = self._async_resolve_ble_device() is not None
        service_info = bluetooth.async_last_service_info(
            self.hass,
            self.address,
            connectable=True,
        )
        if service_info is None:
            service_info = bluetooth.async_last_service_info(
                self.hass,
                self.address,
                connectable=False,
            )
        if service_info is None:
            return

        discovered_name = service_info.name or service_info.device.name
        if discovered_name and self._name == DEFAULT_NAME:
            self._name = discovered_name
        self._rssi = service_info.rssi
        if bluetooth.async_address_present(self.hass, self.address, connectable=False):
            self._last_seen = datetime.now(UTC)

    def _build_state(self) -> AC500RuntimeState:
        """Build the current immutable runtime snapshot."""
        return AC500RuntimeState(
            address=self.address,
            name=self._name,
            status=self._status,
            available=self._status is not None and self._status_ok and self._connectable,
            connectable=self._connectable,
            rssi=self._rssi,
            last_seen=self._last_seen,
            last_error=self._last_error,
            session_state=self._session_state,
            paired=self._paired,
            control_mode_active=self._session_state == SESSION_CONTROL,
        )

    def _publish_state(self) -> None:
        """Refresh discovery metadata and publish current state."""
        self._refresh_discovery_data()
        self.async_set_updated_data(self._build_state())
