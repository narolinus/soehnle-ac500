"""Temporary BLE setup helpers for the Soehnle AC500."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .protocol import (
    ACK_CHAR_UUID,
    LIVE_DATA_CHAR_UUID,
    WRITE_CHAR_UUID,
    AC500Status,
    build_frame,
)

_LOGGER = logging.getLogger(__name__)


class AC500SetupClient:
    """Short-lived setup client used by the config flow."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        ble_device: BLEDevice | None = None,
    ) -> None:
        """Initialize the setup client."""
        self.hass = hass
        self.address = address
        self.name = name
        self.ble_device = ble_device
        self.client: BleakClientWithServiceCache | None = None
        self.live_event = asyncio.Event()
        self.ack_event = asyncio.Event()
        self.last_status: AC500Status | None = None
        self.last_ack: bytes | None = None

    def _resolve_ble_device(self) -> BLEDevice | None:
        """Resolve the freshest known BLE device object."""
        return bluetooth.async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        ) or self.ble_device

    async def __aenter__(self) -> "AC500SetupClient":
        """Connect and start notifications."""
        ble_device = self._resolve_ble_device()
        if ble_device is None:
            raise HomeAssistantError(
                "The AC500 is currently not visible via a connectable Bluetooth adapter or proxy."
            )

        self.client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.name,
            max_attempts=3,
            timeout=20.0,
        )
        await self.client.start_notify(LIVE_DATA_CHAR_UUID, self._handle_live_data)
        await self.client.start_notify(ACK_CHAR_UUID, self._handle_ack)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Disconnect the temporary client."""
        del exc_type, exc, tb
        if self.client is None:
            return
        with contextlib.suppress(Exception):
            if self.client.is_connected:
                await self.client.disconnect()
        self.client = None

    async def async_pair_and_initialize(self) -> AC500Status | None:
        """Run pairing and return an initial status frame."""
        await self._async_pair_backend_if_supported()
        await self.async_run_pairing_handshake()
        try:
            await self.async_enter_control_mode()
            return await self.async_request_status()
        except HomeAssistantError as err:
            _LOGGER.warning(
                "AC500 pairing for %s completed, but the immediate post-pair control session failed: %s",
                self.address,
                err,
            )
            return self.last_status

    async def _async_pair_backend_if_supported(self) -> None:
        """Ask the underlying backend to pair if it can."""
        if self.client is None:
            raise HomeAssistantError("The AC500 is not connected")

        pair_method = getattr(self.client, "pair", None)
        if not callable(pair_method):
            return

        try:
            await asyncio.wait_for(pair_method(), timeout=15.0)
        except TimeoutError:
            _LOGGER.warning("Backend pairing for %s timed out", self.address)
            return
        except Exception as err:
            _LOGGER.warning("Backend pairing for %s failed: %s", self.address, err)
            return

        await asyncio.sleep(0.5)

    async def async_run_pairing_handshake(self, timeout: float = 25.0) -> None:
        """Run the AC500 pairing handshake over EF03."""
        expected_ack = build_frame(0xA2, 0x00, 0x02)
        self.last_ack = None
        self.ack_event.clear()

        deadline = asyncio.get_running_loop().time() + timeout
        ack = None

        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            probe_timeout = min(2.5, remaining)

            _LOGGER.debug("Sending AC500 pairing probe to %s", self.address)
            await self.async_send_frame(0xA2, 0x00, 0x03, expect_status=False)
            ack = await self.async_wait_for_ack(expected_ack, timeout=probe_timeout)
            if ack == expected_ack:
                break

            await asyncio.sleep(0.25)

        if ack != expected_ack:
            raise HomeAssistantError(
                "No AC500 pairing acknowledgement received. Press the Bluetooth button on the purifier and try again."
            )

        _LOGGER.debug("Received AC500 pairing acknowledgement from %s", self.address)
        await asyncio.sleep(0.1)
        await self.async_send_frame(0xA2, 0x00, 0x01, expect_status=False)
        await asyncio.sleep(0.5)

    async def async_enter_control_mode(self) -> AC500Status | None:
        """Open the control session."""
        await self.async_send_frame(0xAF, 0x00, 0x01, expect_status=False)
        await asyncio.sleep(0.1)
        await self.async_send_frame(0xAF, 0x00, 0x01, expect_status=False)

        self.live_event.clear()
        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=2.0)
            return self.last_status
        except TimeoutError:
            return await self.async_request_status()
        finally:
            self.live_event.clear()

    async def async_request_status(self) -> AC500Status | None:
        """Request the current status frame."""
        self.live_event.clear()
        await self.async_send_frame(0xA2, 0x00, 0x03, expect_status=False)
        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=3.0)
        except TimeoutError:
            return self.last_status
        finally:
            self.live_event.clear()
        return self.last_status

    async def async_wait_for_ack(self, expected: bytes, timeout: float) -> bytes | None:
        """Wait for a specific ACK frame."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.last_ack == expected:
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

    async def async_send_frame(
        self,
        opcode: int,
        arg1: int = 0x00,
        arg2: int = 0x00,
        *,
        expect_status: bool = True,
    ) -> AC500Status | None:
        """Send a frame to the device."""
        if self.client is None:
            raise HomeAssistantError("The AC500 is not connected")

        frame = build_frame(opcode, arg1, arg2)
        self.live_event.clear()
        try:
            await self.client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)
        except Exception as err:
            raise HomeAssistantError(f"Sending the BLE command failed: {err}") from err

        if not expect_status:
            return self.last_status

        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=3.0)
        except TimeoutError:
            return self.last_status
        finally:
            self.live_event.clear()
        return self.last_status

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


async def async_pair_ac500(
    hass: HomeAssistant,
    address: str,
    name: str,
    ble_device: BLEDevice | None = None,
) -> AC500Status | None:
    """Pair and initialize one AC500."""
    async with AC500SetupClient(hass, address, name, ble_device=ble_device) as client:
        return await client.async_pair_and_initialize()
