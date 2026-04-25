"""BLE client for Soehnle AC500 devices through Home Assistant Bluetooth."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging

from bleak import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant, callback

from .const import (
    ACK_CHAR_UUID,
    COMMAND_TIMEOUT,
    DEVICE_NAME,
    LIVE_DATA_CHAR_UUID,
    PAIR_TIMEOUT,
    SERVICE_UUID,
    SESSION_TIMEOUT,
    STATE_COMMAND_SENT,
    STATE_COMMAND_TIMEOUT,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_PAIR_ACK,
    STATE_PAIRED,
    STATE_PAIR_TIMEOUT,
    STATE_PAIRING,
    STATE_PARSE_FAILED,
    STATE_STATUS_RECEIVED,
    STATUS_TIMEOUT,
    WRITE_CHAR_UUID,
)
from .protocol import (
    AC500Status,
    AUTO_COMMANDS,
    BUZZER_COMMANDS,
    FAN_VALUES,
    NIGHT_COMMANDS,
    POWER_COMMANDS,
    TIMER_VALUES,
    UV_COMMANDS,
    build_frame,
    is_pair_ack,
)

_LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[], None]


class AC500CommunicationError(Exception):
    """Raised when communicating with an AC500 fails."""


class AC500Device:
    """High-level AC500 BLE session wrapper."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        status_callback: StatusCallback,
    ) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self._status_callback = status_callback
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._live_event = asyncio.Event()
        self._ack_event = asyncio.Event()
        self._last_seen_status_counter = 0

        self.last_status: AC500Status | None = None
        self.last_ack: bytes | None = None
        self.last_error: str | None = None
        self.state = STATE_DISCONNECTED
        self.connected = False
        self.rssi: int | None = None

    async def async_update(self) -> AC500Status | None:
        """Fetch a fresh status frame in a short BLE session."""
        async with self._lock:
            try:
                await self._connect()
                status = await self._initialize_session()
                self.last_error = None
                return status
            finally:
                await self._disconnect()

    async def async_pair(self) -> AC500Status | None:
        """Run BLE pairing, then the observed proprietary AC500 handshake."""
        async with self._lock:
            try:
                await self._connect()
                self.state = STATE_PAIRING
                self._notify()

                pair_method = getattr(self._client, "pair", None)
                if callable(pair_method):
                    with contextlib.suppress(Exception):
                        await pair_method()

                expected = build_frame(0xA2, 0x00, 0x02)
                self.last_ack = None
                self._ack_event.clear()
                await self._write_command(0xA2, 0x00, 0x03)

                ack = await self._wait_for_ack(lambda data: data == expected, PAIR_TIMEOUT)
                if ack != expected:
                    self.state = STATE_PAIR_TIMEOUT
                    self._notify()
                    return self.last_status

                self.state = STATE_PAIR_ACK
                self._notify()
                await asyncio.sleep(0.1)
                await self._write_command(0xA2, 0x00, 0x01)
                await asyncio.sleep(0.3)
                self.state = STATE_PAIRED
                self._notify()
                return await self._initialize_session()
            finally:
                await self._disconnect()

    async def async_reconnect(self) -> AC500Status | None:
        """Disconnect and read status again."""
        async with self._lock:
            await self._disconnect()
            await asyncio.sleep(0.5)
            try:
                await self._connect()
                return await self._initialize_session()
            finally:
                await self._disconnect()

    async def async_set_power(self, enabled: bool) -> AC500Status | None:
        """Turn the purifier power on or off."""
        return await self._run_command(
            *POWER_COMMANDS[enabled],
            predicate=lambda status: status.power_enabled == enabled,
        )

    async def async_set_uv(self, enabled: bool) -> AC500Status | None:
        """Turn UV-C on or off."""
        return await self._run_command(
            *UV_COMMANDS[enabled],
            predicate=lambda status: status.uv_enabled == enabled,
        )

    async def async_set_auto(self, enabled: bool) -> AC500Status | None:
        """Turn automatic mode on or off."""
        return await self._run_command(
            *AUTO_COMMANDS[enabled],
            predicate=lambda status: status.auto_enabled == enabled,
        )

    async def async_set_night(self, enabled: bool) -> AC500Status | None:
        """Turn night mode on or off."""
        return await self._run_command(
            *NIGHT_COMMANDS[enabled],
            predicate=lambda status: status.night_enabled == enabled,
        )

    async def async_set_buzzer(self, enabled: bool) -> AC500Status | None:
        """Turn the buzzer on or off."""
        return await self._run_command(
            *BUZZER_COMMANDS[enabled],
            predicate=lambda status: status.buzzer_enabled == enabled,
        )

    async def async_set_fan_mode(self, mode: str) -> AC500Status | None:
        """Set a fixed fan speed, leaving automatic mode first if needed."""
        fan = FAN_VALUES[mode]

        async with self._lock:
            try:
                await self._connect()
                await self._enter_control_mode()

                if self.last_status is not None and self.last_status.auto_enabled:
                    await self._write_command(*AUTO_COMMANDS[False])
                    await self._wait_for_status(
                        lambda status: not status.auto_enabled,
                        COMMAND_TIMEOUT,
                    )
                    await asyncio.sleep(0.25)

                await self._write_command(0x02, 0x00, fan)
                status = await self._wait_for_status(
                    lambda item: item.fan_raw == fan and not item.auto_enabled,
                    COMMAND_TIMEOUT,
                )
                if status is None:
                    self.state = STATE_COMMAND_TIMEOUT
                    status = await self._request_status()
                else:
                    self.state = STATE_COMMAND_SENT
                self.last_error = None
                self._notify()
                return status
            finally:
                await self._disconnect()

    async def async_set_timer(self, option: str) -> AC500Status | None:
        """Set the timer option."""
        timer = TIMER_VALUES[option]
        return await self._run_command(
            0x04,
            0x00,
            timer,
            predicate=lambda status: status.timer_raw == timer,
        )

    async def async_shutdown(self) -> None:
        """Close an open BLE connection."""
        async with self._lock:
            await self._disconnect()

    async def _run_command(
        self,
        opcode: int,
        arg1: int,
        arg2: int,
        predicate: Callable[[AC500Status], bool],
    ) -> AC500Status | None:
        """Run one control-mode command and refresh status."""
        async with self._lock:
            try:
                await self._connect()
                await self._enter_control_mode()
                await self._write_command(opcode, arg1, arg2)
                status = await self._wait_for_status(predicate, COMMAND_TIMEOUT)
                if status is None:
                    self.state = STATE_COMMAND_TIMEOUT
                    status = await self._request_status()
                else:
                    self.state = STATE_COMMAND_SENT
                self.last_error = None
                self._notify()
                return status
            finally:
                await self._disconnect()

    async def _connect(self) -> None:
        """Open a BLE connection and subscribe to notifications."""
        if self._client is not None and self._client.is_connected:
            return

        ble_device = async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )
        if ble_device is None:
            raise AC500CommunicationError(
                f"{self.name} is not currently visible to Home Assistant Bluetooth"
            )

        self.rssi = getattr(ble_device, "rssi", None)

        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.name or DEVICE_NAME,
                self._handle_disconnect,
                ble_device_callback=lambda: async_ble_device_from_address(
                    self.hass,
                    self.address,
                    connectable=True,
                ),
                use_services_cache=True,
                timeout=SESSION_TIMEOUT,
            )
            await self._client.start_notify(LIVE_DATA_CHAR_UUID, self._handle_live_data)
            await self._client.start_notify(ACK_CHAR_UUID, self._handle_ack)
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            self.last_error = str(err)
            self.state = STATE_DISCONNECTED
            self.connected = False
            self._notify()
            raise AC500CommunicationError(str(err)) from err

        self.connected = True
        self.state = STATE_CONNECTED
        self._notify()

    async def _disconnect(self) -> None:
        """Disconnect from the purifier."""
        client = self._client
        self._client = None
        if client is not None and client.is_connected:
            with contextlib.suppress(Exception):
                await client.disconnect()

        self.connected = False
        if self.state == STATE_CONNECTED:
            self.state = STATE_DISCONNECTED
        self._notify()

    async def _initialize_session(self) -> AC500Status | None:
        """Run the lightweight status session from the working implementations."""
        await self._write_command(0xAF, 0x00, 0x01)
        await asyncio.sleep(0.2)
        return await self._request_status()

    async def _enter_control_mode(self) -> AC500Status | None:
        """Enter the observed control mode before sending commands."""
        start_counter = self._last_seen_status_counter
        await self._write_command(0xAF, 0x00, 0x01)
        await asyncio.sleep(0.12)
        await self._write_command(0xAF, 0x00, 0x01)

        try:
            await asyncio.wait_for(self._live_event.wait(), timeout=2.0)
        except TimeoutError:
            return await self._request_status()
        finally:
            self._live_event.clear()

        if self._last_seen_status_counter == start_counter:
            return await self._request_status()

        await asyncio.sleep(0.2)
        return self.last_status

    async def _request_status(self) -> AC500Status | None:
        """Request one live status notification."""
        self._live_event.clear()
        await self._write_command(0xA2, 0x00, 0x03)
        try:
            await asyncio.wait_for(self._live_event.wait(), timeout=STATUS_TIMEOUT)
        except TimeoutError:
            return self.last_status
        finally:
            self._live_event.clear()
        return self.last_status

    async def _write_command(self, opcode: int, arg1: int = 0, arg2: int = 0) -> None:
        """Write one framed command to EF01."""
        if self._client is None or not self._client.is_connected:
            raise AC500CommunicationError("BLE client is not connected")

        frame = build_frame(opcode, arg1, arg2)
        _LOGGER.debug("%s TX %s", self.address, frame.hex())
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)
        except Exception as err:
            self.last_error = str(err)
            raise AC500CommunicationError(str(err)) from err

    async def _wait_for_ack(
        self,
        predicate: Callable[[bytes], bool],
        timeout: float,
    ) -> bytes | None:
        """Wait until an ACK notification matches a predicate."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.last_ack is not None and predicate(self.last_ack):
                return self.last_ack

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self.last_ack

            self._ack_event.clear()
            try:
                await asyncio.wait_for(self._ack_event.wait(), timeout=remaining)
            except TimeoutError:
                return self.last_ack
            finally:
                self._ack_event.clear()

    async def _wait_for_status(
        self,
        predicate: Callable[[AC500Status], bool],
        timeout: float,
    ) -> AC500Status | None:
        """Wait for a status matching a predicate, polling if notifications stall."""
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            if self.last_status is not None and predicate(self.last_status):
                return self.last_status

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if self.last_status is not None and predicate(self.last_status):
                    return self.last_status
                return None

            self._live_event.clear()
            try:
                await asyncio.wait_for(self._live_event.wait(), timeout=min(1.0, remaining))
            except TimeoutError:
                await self._request_status()
            finally:
                self._live_event.clear()

    def _handle_live_data(
        self,
        _characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        """Handle EF02 notifications."""
        frame = bytes(data)
        _LOGGER.debug("%s RX live %s", self.address, frame.hex())
        try:
            self.last_status = AC500Status.from_frame(frame)
        except ValueError:
            self.state = STATE_PARSE_FAILED
            self._notify()
            return

        self._last_seen_status_counter += 1
        self.state = STATE_STATUS_RECEIVED
        self._live_event.set()
        self._notify()

    def _handle_ack(
        self,
        _characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        """Handle EF03 notifications."""
        self.last_ack = bytes(data)
        _LOGGER.debug("%s RX ack %s", self.address, self.last_ack.hex())
        if is_pair_ack(self.last_ack):
            self.state = STATE_PAIR_ACK
        self._ack_event.set()
        self._notify()

    @callback
    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Handle a BLE disconnect callback."""
        self.connected = False
        self.state = STATE_DISCONNECTED
        self._client = None
        self._notify()

    @callback
    def _notify(self) -> None:
        """Notify Home Assistant entities that device data changed."""
        self._status_callback()

    @property
    def last_ack_hex(self) -> str | None:
        """Return the last ACK notification as hex."""
        return self.last_ack.hex() if self.last_ack else None

    @property
    def last_frame_hex(self) -> str | None:
        """Return the last live frame as hex."""
        return self.last_status.raw_frame_hex if self.last_status else None

    @property
    def service_uuid(self) -> str:
        """Return the primary AC500 service UUID."""
        return SERVICE_UUID
