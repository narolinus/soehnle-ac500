"""BLE session helpers for the Soehnle AC500."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONNECT_TIMEOUT,
    CONTROL_ENTER_DELAY,
    CONTROL_SETTLE_DELAY,
    PAIRING_TIMEOUT,
    STATUS_TIMEOUT,
)
from .protocol import (
    ACK_CHAR_UUID,
    LIVE_DATA_CHAR_UUID,
    WRITE_CHAR_UUID,
    AC500Status,
    build_frame,
)

_LOGGER = logging.getLogger(__name__)


class AC500BleSession:
    """Short-lived BLE session for one AC500 operation."""

    def __init__(
        self,
        address: str,
        name: str,
        ble_device_callback: Callable[[], BLEDevice | None],
    ) -> None:
        """Initialize the BLE session."""
        self.address = address
        self.name = name
        self._ble_device_callback = ble_device_callback
        self.client: BleakClient | None = None
        self.last_status: AC500Status | None = None
        self.last_ack: bytes | None = None
        self.live_event = asyncio.Event()
        self.ack_event = asyncio.Event()

    async def __aenter__(self) -> "AC500BleSession":
        """Connect and subscribe to notifications."""
        ble_device = self._ble_device_callback()
        if ble_device is None:
            raise HomeAssistantError(
                "The AC500 is currently not reachable via a connectable Bluetooth adapter or proxy."
            )

        try:
            self.client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.name,
                ble_device_callback=self._ble_device_callback,
                max_attempts=3,
                timeout=CONNECT_TIMEOUT,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Connecting to the AC500 via Home Assistant Bluetooth failed: {err}"
            ) from err

        await self._async_safe_start_notify(LIVE_DATA_CHAR_UUID, self._handle_live_data)
        await self._async_safe_start_notify(ACK_CHAR_UUID, self._handle_ack)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Release notifications and disconnect."""
        del exc_type, exc, tb
        client = self.client
        self.client = None
        if client is None:
            return

        with contextlib.suppress(Exception):
            await client.stop_notify(LIVE_DATA_CHAR_UUID)
        with contextlib.suppress(Exception):
            await client.stop_notify(ACK_CHAR_UUID)
        with contextlib.suppress(Exception):
            if client.is_connected:
                await client.disconnect()

    async def _async_safe_start_notify(
        self,
        uuid: str,
        callback: Callable[[Any, bytearray], None],
    ) -> None:
        """Subscribe to notifications with one stale-lock retry."""
        client = self.client
        if client is None:
            raise HomeAssistantError("The AC500 is not connected.")

        with contextlib.suppress(Exception):
            await client.stop_notify(uuid)
        await asyncio.sleep(0.1)

        try:
            await client.start_notify(uuid, callback)
        except Exception as err:
            text = str(err)
            if "Notify acquired" not in text and "NotPermitted" not in text:
                raise HomeAssistantError(f"Starting notifications failed: {err}") from err

            _LOGGER.debug(
                "AC500 %s: retrying start_notify for %s after stale notify lock",
                self.address,
                uuid,
            )
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                await client.stop_notify(uuid)
            await asyncio.sleep(0.5)
            try:
                await client.start_notify(uuid, callback)
            except Exception as retry_err:
                raise HomeAssistantError(
                    f"Starting notifications failed after retry: {retry_err}"
                ) from retry_err

    async def async_ensure_ble_link_paired(self) -> None:
        """Attempt normal BLE link pairing if the backend exposes it."""
        client = self.client
        if client is None:
            raise HomeAssistantError("The AC500 is not connected.")

        pair_method = getattr(client, "pair", None)
        if not callable(pair_method):
            _LOGGER.debug("AC500 %s: backend does not expose pair()", self.address)
            return

        try:
            await pair_method()
            await asyncio.sleep(0.5)
        except Exception as err:
            text = str(err).lower()
            if (
                "already" in text and "pair" in text
            ) or "alreadyexists" in text or "already bonded" in text:
                return
            raise HomeAssistantError(f"BLE link pairing failed: {err}") from err

    async def async_write_frame(
        self,
        opcode: int,
        arg1: int = 0x00,
        arg2: int = 0x00,
    ) -> None:
        """Write one protocol frame."""
        client = self.client
        if client is None:
            raise HomeAssistantError("The AC500 is not connected.")

        frame = build_frame(opcode, arg1, arg2)
        _LOGGER.debug("AC500 %s TX %s", self.address, frame.hex())
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)
        except Exception as err:
            raise HomeAssistantError(f"Sending the BLE command failed: {err}") from err

    async def async_initialize_status_channel(self) -> AC500Status | None:
        """Initialize a status-only session and fetch one live frame."""
        await self.async_write_frame(0xAF, 0x00, 0x01)
        await asyncio.sleep(CONTROL_SETTLE_DELAY)
        return await self.async_request_status()

    async def async_enter_control_mode(self) -> AC500Status | None:
        """Open the control mode connection used for writes."""
        await self.async_write_frame(0xAF, 0x00, 0x01)
        await asyncio.sleep(CONTROL_ENTER_DELAY)
        await self.async_write_frame(0xAF, 0x00, 0x01)

        self.live_event.clear()
        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=2.0)
        except TimeoutError:
            return await self.async_request_status()
        finally:
            self.live_event.clear()

        await asyncio.sleep(CONTROL_SETTLE_DELAY)
        return self.last_status

    async def async_request_status(self) -> AC500Status | None:
        """Request one fresh live status frame."""
        self.live_event.clear()
        await self.async_write_frame(0xA2, 0x00, 0x03)
        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=STATUS_TIMEOUT)
        except TimeoutError:
            return self.last_status
        finally:
            self.live_event.clear()
        return self.last_status

    async def async_wait_for_ack(
        self,
        predicate: Callable[[bytes], bool],
        *,
        timeout: float = PAIRING_TIMEOUT,
    ) -> bytes | None:
        """Wait for one matching ACK frame."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.last_ack is not None and predicate(self.last_ack):
                return self.last_ack

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self.last_ack

            self.ack_event.clear()
            try:
                await asyncio.wait_for(self.ack_event.wait(), timeout=remaining)
            except TimeoutError:
                return self.last_ack
            finally:
                self.ack_event.clear()

    async def async_wait_for_status(
        self,
        predicate: Callable[[AC500Status], bool],
        *,
        timeout: float,
        refresh_interval: float = 1.0,
    ) -> AC500Status | None:
        """Wait until a live frame matches the expected state."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.last_status is not None and predicate(self.last_status):
                return self.last_status

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self.last_status

            self.live_event.clear()
            try:
                await asyncio.wait_for(
                    self.live_event.wait(), timeout=min(refresh_interval, remaining)
                )
                continue
            except TimeoutError:
                await self.async_request_status()
            finally:
                self.live_event.clear()

    async def async_run_pairing_handshake(self, timeout: float = PAIRING_TIMEOUT) -> None:
        """Run the AC500-specific EF03 pairing handshake."""
        await self.async_ensure_ble_link_paired()
        expected_ack = build_frame(0xA2, 0x00, 0x02)
        self.last_ack = None
        self.ack_event.clear()

        await self.async_write_frame(0xA2, 0x00, 0x03)
        ack = await self.async_wait_for_ack(lambda data: data == expected_ack, timeout=timeout)
        if ack != expected_ack:
            raise HomeAssistantError(
                "No AC500 pairing acknowledgement was received. Press the Bluetooth button on the purifier while the Pair action is running."
            )

        await asyncio.sleep(0.1)
        await self.async_write_frame(0xA2, 0x00, 0x01)
        await asyncio.sleep(0.3)

    def _handle_live_data(self, _characteristic: Any, data: bytearray) -> None:
        """Handle live status notifications."""
        try:
            self.last_status = AC500Status.from_frame(bytes(data))
        except ValueError:
            return
        self.live_event.set()

    def _handle_ack(self, _characteristic: Any, data: bytearray) -> None:
        """Handle ACK notifications."""
        self.last_ack = bytes(data)
        self.ack_event.set()
