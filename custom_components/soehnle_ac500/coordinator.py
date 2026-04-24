"""Coordinator and connection manager for the Soehnle AC500."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothChange, BluetoothScanningMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_KEEPALIVE_SECONDS,
    CONF_RECONNECT_SECONDS,
    DEFAULT_KEEPALIVE_SECONDS,
    DEFAULT_NAME,
    DEFAULT_RECONNECT_SECONDS,
    DOMAIN,
    FAN_LEVELS,
    TIMER_OPTIONS,
)
from .protocol import (
    ACK_CHAR_UUID,
    AUTO_COMMANDS as PROTOCOL_AUTO_COMMANDS,
    BUZZER_COMMANDS as PROTOCOL_BUZZER_COMMANDS,
    DISCOVERY_SERVICE_UUID,
    FAN_COMMANDS,
    LIVE_DATA_CHAR_UUID,
    MANUFACTURER_ID,
    NIGHT_COMMANDS,
    POWER_COMMANDS,
    TIMER_COMMANDS,
    UV_COMMANDS,
    WRITE_CHAR_UUID,
    AC500Status,
    build_frame,
)

_LOGGER = logging.getLogger(__name__)

# Reconnect timing.
_MIN_RECONNECT_DELAY = 4.0
_MAX_RECONNECT_DELAY = 120.0

# How many consecutive keepalive failures before we drop the session.
_KEEPALIVE_FAIL_LIMIT = 3

# BLE connection timeout — must be long enough for HA's Bluetooth stack
# to complete connect + GATT service discovery, even if the connection
# request is queued behind other devices by ESPHome or BlueZ.
_CONNECT_TIMEOUT = 120.0


@dataclass(slots=True, frozen=True)
class AC500RuntimeState:
    """Published runtime snapshot."""

    address: str
    name: str
    status: AC500Status | None
    available: bool
    connected: bool
    rssi: int | None
    last_seen: datetime | None
    last_error: str | None


class AC500ConnectionManager:
    """Maintain a persistent AC500 BLE connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        update_callback,
        *,
        reconnect_seconds: int = DEFAULT_RECONNECT_SECONDS,
        keepalive_seconds: int = DEFAULT_KEEPALIVE_SECONDS,
    ) -> None:
        """Initialize the connection manager."""
        self.hass = hass
        self.address = address
        self.name = name
        self._update_callback = update_callback
        self._reconnect_seconds = reconnect_seconds
        self._keepalive_seconds = keepalive_seconds

        self._client: BleakClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._live_event = asyncio.Event()
        self._ack_event = asyncio.Event()
        self._operation_lock = asyncio.Lock()
        self._disconnect_event: asyncio.Event | None = None
        self._unsub_bluetooth = None

        self._last_status: AC500Status | None = None
        self._last_ack: bytes | None = None
        self._rssi: int | None = None
        self._last_seen: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures: int = 0
        self._handshake_done: bool = False
        self._ble_link_paired: bool = False

    @property
    def state(self) -> AC500RuntimeState:
        """Return the current published state."""
        return AC500RuntimeState(
            address=self.address,
            name=self.name,
            status=self._last_status,
            available=self._connected_event.is_set() and self._last_status is not None,
            connected=self._connected_event.is_set(),
            rssi=self._rssi,
            last_seen=self._last_seen,
            last_error=self._last_error,
        )

    async def async_start(self) -> None:
        """Start background processing."""
        if self._task is not None:
            return

        self._prime_from_last_service_info()
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_handle_bluetooth_event,
            {"address": self.address, "connectable": True},
            BluetoothScanningMode.ACTIVE,
        )
        self._task = self.hass.async_create_background_task(
            self._async_connection_loop(),
            f"{DOMAIN}_connection_{self.address}",
        )
        self._publish_state()

    async def async_stop(self) -> None:
        """Stop background processing."""
        self._stop_event.set()
        self._wake_event.set()

        if self._unsub_bluetooth is not None:
            self._unsub_bluetooth()
            self._unsub_bluetooth = None

        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        await self._async_disconnect()
        self._publish_state()

    async def async_force_refresh(self) -> AC500RuntimeState:
        """Actively request a fresh status frame if connected."""
        async with self._operation_lock:
            await self._async_ensure_connected()
            status = await self._async_request_status_unlocked()
            if status is None:
                raise HomeAssistantError("No status frame received from the AC500")
        return self.state

    async def async_set_power(self, enabled: bool) -> None:
        """Set purifier power."""
        await self._async_execute_command(
            POWER_COMMANDS["on" if enabled else "off"],
            lambda status: status.power_enabled is enabled,
        )

    async def async_set_uv(self, enabled: bool) -> None:
        """Set UV mode."""
        await self._async_execute_command(
            UV_COMMANDS["on" if enabled else "off"],
            lambda status: status.uv_enabled is enabled,
        )

    async def async_set_auto(self, enabled: bool) -> None:
        """Set auto mode."""
        await self._async_execute_command(
            PROTOCOL_AUTO_COMMANDS["on" if enabled else "off"],
            lambda status: status.auto_enabled is enabled,
        )

    async def async_set_night(self, enabled: bool) -> None:
        """Set night mode."""
        await self._async_execute_command(
            NIGHT_COMMANDS["on" if enabled else "off"],
            lambda status: status.night_enabled is enabled,
        )

    async def async_set_buzzer(self, enabled: bool) -> None:
        """Set hidden buzzer mode."""
        await self._async_execute_command(
            PROTOCOL_BUZZER_COMMANDS["on" if enabled else "off"],
            lambda status: status.buzzer_enabled is enabled,
        )

    async def async_set_timer(self, option: str) -> None:
        """Set timer option."""
        await self._async_execute_command(
            TIMER_COMMANDS[option],
            lambda status, expected=option: status.timer_label == expected,
        )

    async def async_set_fan_level(self, level: str) -> None:
        """Set fan speed."""
        if level not in FAN_LEVELS:
            raise HomeAssistantError(f"Unsupported fan level: {level}")

        async with self._operation_lock:
            await self._async_ensure_connected()

            # Re-enter control mode before sending the command.
            await self._async_enter_control_mode_unlocked()

            if self._last_status is not None and self._last_status.auto_enabled:
                await self._async_write_frame_unlocked(
                    *PROTOCOL_AUTO_COMMANDS["off"],
                )
                await self._async_wait_for_status(
                    lambda status: not status.auto_enabled,
                    timeout=5.0,
                )

            await self._async_write_frame_unlocked(*FAN_COMMANDS[level])
            status = await self._async_wait_for_status(
                lambda result, expected=level: result.fan_label == expected and not result.auto_enabled,
                timeout=5.0,
            )
            if status is None or status.fan_label != level:
                raise HomeAssistantError(
                    "The AC500 did not confirm the requested fan level. "
                    "If control is not active yet, run the Pair action in Home Assistant "
                    "and press the Bluetooth button on the purifier."
                )
            self._ble_link_paired = True
            self._handshake_done = True

    async def async_run_pairing_handshake(self) -> None:
        """Run the AC500 onboarding handshake on the active connection."""
        async with self._operation_lock:
            await self._async_ensure_connected()
            await self._async_run_handshake_unlocked()
            await self._async_request_status_unlocked()

    async def async_reconnect(self) -> None:
        """Force a reconnect cycle."""
        self._trigger_reconnect()

    async def _async_run_handshake_unlocked(self, timeout: float = 20.0) -> None:
        """Execute the EF03 pairing handshake (must hold _operation_lock)."""
        await self._async_ensure_ble_link_paired_unlocked()
        expected_ack = build_frame(0xA2, 0x00, 0x02)
        self._last_ack = None
        self._ack_event.clear()

        _LOGGER.debug("Starting AC500 EF03 handshake for %s", self.address)
        await self._async_write_frame_unlocked(0xA2, 0x00, 0x03)
        ack = await self._async_wait_for_ack(expected_ack, timeout=timeout)
        if ack != expected_ack:
            raise HomeAssistantError(
                "No AC500 pairing acknowledgement received. Ensure the purifier is powered on and in range."
            )

        _LOGGER.debug("Received AC500 pairing ack from %s", self.address)
        await asyncio.sleep(0.1)
        await self._async_write_frame_unlocked(0xA2, 0x00, 0x01)
        await asyncio.sleep(0.3)
        self._handshake_done = True

    async def _async_execute_command(self, frame: tuple[int, int, int], predicate) -> None:
        """Send a command and wait until the status reflects it.

        Mirrors the CLI pattern: re-enter control mode (AF 00 01) before each
        command to ensure the device is ready to accept writes.
        """
        async with self._operation_lock:
            await self._async_ensure_connected()
            await self._async_enter_control_mode_unlocked()
            await self._async_write_frame_unlocked(*frame)
            status = await self._async_wait_for_status(predicate, timeout=5.0)
            if status is None or not predicate(status):
                raise HomeAssistantError(
                    "The AC500 did not confirm the requested state change. "
                    "If control is not active yet, run the Pair action in Home Assistant "
                    "and press the Bluetooth button on the purifier."
                )
            self._ble_link_paired = True
            self._handshake_done = True

    def _trigger_reconnect(self) -> None:
        """Force an immediate reconnect cycle."""
        self._wake_event.set()
        if self._disconnect_event is not None:
            self._disconnect_event.set()

    async def _async_connection_loop(self) -> None:
        """Maintain a live connection when the device is present."""
        while not self._stop_event.is_set():
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass,
                self.address,
                connectable=True,
            )

            if ble_device is None:
                self._clear_connected_state()
                self._publish_state()
                await self._async_wait_for_wake(self._reconnect_seconds)
                continue

            try:
                await self._async_run_connected_session(ble_device)
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._last_error = str(err)
                self._consecutive_failures += 1
                _LOGGER.debug(
                    "AC500 session for %s ended (failure #%d): %s",
                    self.address,
                    self._consecutive_failures,
                    err,
                )
            finally:
                await self._async_disconnect()
                self._clear_connected_state()
                self._publish_state()

            # Exponential backoff to avoid exhausting proxy connection slots.
            # _MIN_RECONNECT_DELAY * 2^failures, capping at _MAX_RECONNECT_DELAY.
            delay = min(
                _MIN_RECONNECT_DELAY * (2 ** min(self._consecutive_failures, 5)),
                _MAX_RECONNECT_DELAY,
            )
            _LOGGER.debug(
                "AC500 %s: reconnect in %.0fs (failure #%d)",
                self.address,
                delay,
                self._consecutive_failures,
            )
            await self._async_wait_for_wake(delay)

    async def _async_run_connected_session(self, ble_device: BLEDevice) -> None:
        """Open and hold a connection until it drops."""
        disconnected_event = asyncio.Event()
        self._disconnect_event = disconnected_event

        self._client = await self._async_connect_via_ha_bluetooth(ble_device, disconnected_event)
        self._last_error = None

        # Immediately subscribe to notifications — same approach as the CLI.
        # No redundant get_services() call: establish_connection already
        # resolves GATT services during connect().  The device drops idle
        # connections after ~10 s, so every millisecond counts.
        _LOGGER.debug(
            "AC500 %s: connected, services resolved: %s",
            self.address,
            getattr(self._client, "services", None) is not None,
        )
        await self._async_safe_start_notify(self._client, LIVE_DATA_CHAR_UUID, self._handle_live_data)
        await self._async_safe_start_notify(self._client, ACK_CHAR_UUID, self._handle_ack)

        self._connected_event.set()
        self._publish_state()

        # Keepalive loop.  Tolerate multiple consecutive failures before
        # dropping the session; a single proxy hiccup should not kill us.
        keepalive_misses = 0
        while not self._stop_event.is_set() and not disconnected_event.is_set():
            self._live_event.clear()
            try:
                await asyncio.wait_for(
                    self._live_event.wait(),
                    timeout=self._keepalive_seconds,
                )
                keepalive_misses = 0
                continue
            except TimeoutError:
                pass
            finally:
                self._live_event.clear()

            # No notification — try to re-enter control mode and request
            # status.  Errors are swallowed and counted as misses.
            try:
                async with self._operation_lock:
                    await self._async_enter_control_mode_unlocked()
                    status = await self._async_request_status_unlocked()
                if status is not None:
                    keepalive_misses = 0
                    continue
            except HomeAssistantError as err:
                _LOGGER.debug("AC500 %s keepalive write failed: %s", self.address, err)

            keepalive_misses += 1
            _LOGGER.debug(
                "AC500 %s keepalive miss #%d/%d",
                self.address,
                keepalive_misses,
                _KEEPALIVE_FAIL_LIMIT,
            )
            if keepalive_misses >= _KEEPALIVE_FAIL_LIMIT:
                raise RuntimeError(
                    f"No status after {_KEEPALIVE_FAIL_LIMIT} keepalive attempts"
                )

    async def _async_wait_for_wake(self, timeout: float) -> None:
        """Wait for a wake event or timeout."""
        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except TimeoutError:
            return
        finally:
            self._wake_event.clear()

    async def _async_disconnect(self) -> None:
        """Disconnect the current client.

        ALWAYS call stop_notify — even if is_connected is False.
        BlueZ may still hold the notification file descriptors after
        an unclean disconnect (supervision timeout, proxy reset, etc.).
        """
        client = self._client
        self._client = None
        if client is None:
            return
        # Release notification FDs regardless of connection state.
        with contextlib.suppress(Exception):
            await client.stop_notify(LIVE_DATA_CHAR_UUID)
        with contextlib.suppress(Exception):
            await client.stop_notify(ACK_CHAR_UUID)
        with contextlib.suppress(Exception):
            if client.is_connected:
                await client.disconnect()

    def _clear_connected_state(self) -> None:
        """Clear connection-related runtime state."""
        self._connected_event.clear()
        self._disconnect_event = None
        self._ack_event.clear()
        self._live_event.clear()

    async def _async_ensure_connected(self) -> None:
        """Ensure an active connection exists."""
        if self._connected_event.is_set() and self._client is not None:
            return

        self._wake_event.set()
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=self._reconnect_seconds + 10)
        except TimeoutError as err:
            raise HomeAssistantError("No connectable AC500 is currently available") from err

        if self._client is None:
            raise HomeAssistantError("The AC500 connection is not ready")

    def _async_get_current_ble_device(self) -> BLEDevice | None:
        """Return the freshest known BLE device object."""
        return bluetooth.async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )

    async def _async_safe_start_notify(self, client: BleakClient, uuid: str, callback) -> None:
        """Subscribe to notifications, aggressively clearing stale BlueZ state.

        BlueZ keeps exclusive notification file descriptors.  If the previous
        connection was not cleanly torn down, the FD is still held and
        start_notify raises ``NotPermitted: Notify acquired``.

        Strategy:
        1. Try bleak's stop_notify (handles THIS client's FDs).
        2. Try bleak's start_notify.
        3. On failure, call BlueZ's StopNotify D-Bus method DIRECTLY
           (handles ANY client's stale FDs) and retry start_notify.
        """
        # 1. Preemptive release via bleak.
        with contextlib.suppress(Exception):
            await client.stop_notify(uuid)

        # 2. Try normal subscription.
        try:
            await client.start_notify(uuid, callback)
            return  # Success!
        except Exception as err:
            if "Notify acquired" not in str(err) and "NotPermitted" not in str(err):
                raise  # Non-notify error — pass through.

            _LOGGER.warning(
                "AC500 %s: 'Notify acquired' for %s — "
                "calling BlueZ StopNotify directly via D-Bus",
                self.address,
                uuid,
            )

        # 3. Nuclear option: call BlueZ StopNotify via D-Bus directly.
        await self._async_force_bluez_stop_notify(client, uuid)
        await asyncio.sleep(0.3)
        await client.start_notify(uuid, callback)

    async def _async_force_bluez_stop_notify(self, client: BleakClient, char_uuid: str) -> None:
        """Call StopNotify on the BlueZ D-Bus characteristic object directly.

        This bypasses bleak's internal FD tracking and forces BlueZ to
        release the notification for ANY client that holds it.
        """
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast import Message, MessageType

            # Resolve the D-Bus object path for this characteristic.
            char_path = self._resolve_char_dbus_path(client, char_uuid)
            if char_path is None:
                _LOGGER.debug(
                    "AC500 %s: could not resolve D-Bus path for %s",
                    self.address, char_uuid,
                )
                return

            _LOGGER.debug(
                "AC500 %s: calling StopNotify on %s",
                self.address, char_path,
            )
            bus = await MessageBus(bus_type=2).connect()  # 2 = system bus
            try:
                reply = await bus.call(
                    Message(
                        destination="org.bluez",
                        path=char_path,
                        interface="org.bluez.GattCharacteristic1",
                        member="StopNotify",
                    )
                )
                if reply.message_type == MessageType.ERROR:
                    _LOGGER.debug(
                        "AC500 %s: StopNotify error: %s %s",
                        self.address, reply.error_name, reply.body,
                    )
            finally:
                bus.disconnect()
        except Exception as err:
            _LOGGER.debug(
                "AC500 %s: direct BlueZ StopNotify failed: %s",
                self.address, err,
            )

    @staticmethod
    def _resolve_char_dbus_path(client: BleakClient, char_uuid: str) -> str | None:
        """Resolve the BlueZ D-Bus object path for a GATT characteristic."""
        services = getattr(client, "services", None)
        if services is None:
            return None
        for char in services.characteristics.values():
            if char.uuid == char_uuid:
                # bleak stores the D-Bus path as the characteristic's 'path' or 'obj' attr.
                path = getattr(char, "path", None) or getattr(char, "obj", {}).get("Path")
                return path
        return None

    async def _async_connect_via_ha_bluetooth(
        self,
        ble_device: BLEDevice,
        disconnected_event: asyncio.Event,
    ) -> BleakClient:
        """Connect using Home Assistant's Bluetooth stack.

        MUST use establish_connection with BleakClientWithServiceCache.
        Proxies (like ESPHome) take >10 seconds to discover GATT services
        initially. The AC500 drops idle connections after exactly 10s.
        If we don't cache services, the connection will always time out
        mid-discovery before we can initialize control mode.
        """
        import time as _time

        def _handle_disconnect(_: Any) -> None:
            self.hass.loop.call_soon_threadsafe(disconnected_event.set)

        _LOGGER.debug(
            "AC500 %s: connecting via HA establish_connection",
            self.address,
        )
        t0 = _time.monotonic()

        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.name,
                ble_device_callback=self._async_get_current_ble_device,
                disconnected_callback=_handle_disconnect,
                max_attempts=3,
                timeout=_CONNECT_TIMEOUT,
            )
            elapsed = _time.monotonic() - t0
            _LOGGER.debug(
                "AC500 %s: connected in %.1fs",
                self.address,
                elapsed,
            )
            return client
        except Exception as err:
            elapsed = _time.monotonic() - t0
            _LOGGER.warning(
                "AC500 %s connect failed after %.1fs: %s: %s",
                self.address,
                elapsed,
                type(err).__name__,
                err,
            )
            raise HomeAssistantError(
                f"Connecting to the AC500 via Home Assistant Bluetooth failed: {err}"
            ) from err


    async def _async_enter_control_mode_unlocked(self) -> AC500Status | None:
        """Enter the AC500 control session.

        Must be called before sending control commands.  This is resilient:
        write errors are logged and swallowed; we only care about whether
        the device responds with a status notification.
        """
        if not self._ble_link_paired:
            _LOGGER.warning(
                "AC500 %s: BLE link is not marked paired; live status may work while control stays unauthorized",
                self.address,
            )
        for _ in range(2):
            try:
                await self._async_write_frame_unlocked(0xAF, 0x00, 0x01)
            except HomeAssistantError as err:
                _LOGGER.debug("AC500 %s: control mode write failed: %s", self.address, err)
            await asyncio.sleep(0.1)

        self._live_event.clear()
        try:
            await asyncio.wait_for(self._live_event.wait(), timeout=2.0)
        except TimeoutError:
            try:
                return await self._async_request_status_unlocked()
            except HomeAssistantError:
                return self._last_status
        finally:
            self._live_event.clear()

        # Small settle delay after receiving the status frame, matching the
        # CLI's enter_control_mode behaviour.
        await asyncio.sleep(0.2)
        return self._last_status

    async def _async_request_status_unlocked(self) -> AC500Status | None:
        """Request one fresh live status frame."""
        self._live_event.clear()
        await self._async_write_frame_unlocked(0xA2, 0x00, 0x03)
        try:
            await asyncio.wait_for(self._live_event.wait(), timeout=3.0)
        except TimeoutError:
            return self._last_status
        finally:
            self._live_event.clear()
        return self._last_status

    async def _async_wait_for_ack(self, expected: bytes, timeout: float) -> bytes | None:
        """Wait for a specific ACK frame."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._last_ack == expected:
                return self._last_ack

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self._last_ack

            self._ack_event.clear()
            try:
                await asyncio.wait_for(self._ack_event.wait(), timeout=remaining)
            except TimeoutError:
                return self._last_ack
            finally:
                self._ack_event.clear()

    async def _async_wait_for_status(self, predicate, timeout: float) -> AC500Status | None:
        """Wait until the current status matches a predicate."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._last_status is not None and predicate(self._last_status):
                return self._last_status

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self._last_status

            self._live_event.clear()
            try:
                await asyncio.wait_for(self._live_event.wait(), timeout=min(1.0, remaining))
            except TimeoutError:
                try:
                    await self._async_request_status_unlocked()
                except HomeAssistantError:
                    pass
            finally:
                self._live_event.clear()

    async def _async_ensure_ble_link_paired_unlocked(self) -> None:
        """Ensure the BLE link itself is paired/bonded."""
        if self._ble_link_paired:
            return

        client = self._client
        if client is None:
            raise HomeAssistantError("The AC500 is not connected")

        pair_method = getattr(client, "pair", None)
        if not callable(pair_method):
            _LOGGER.debug(
                "AC500 %s: bleak backend does not expose pair(); continuing without explicit BLE link pairing",
                self.address,
            )
            return

        _LOGGER.info("AC500 %s: requesting BLE link pairing/bonding", self.address)
        try:
            await pair_method()
            await asyncio.sleep(0.5)
        except Exception as err:
            text = str(err).lower()
            if (
                "already" in text and "pair" in text
            ) or "alreadyexists" in text or "already bonded" in text:
                _LOGGER.debug(
                    "AC500 %s: BLE link was already paired: %s",
                    self.address,
                    err,
                )
            else:
                raise HomeAssistantError(f"BLE link pairing failed: {err}") from err

        self._ble_link_paired = True

    async def _async_write_frame_unlocked(
        self,
        opcode: int,
        arg1: int = 0x00,
        arg2: int = 0x00,
    ) -> None:
        """Write one protocol frame."""
        client = self._client
        if client is None:
            raise HomeAssistantError("The AC500 is not connected")

        frame = build_frame(opcode, arg1, arg2)
        _LOGGER.debug("AC500 %s TX %s", self.address, frame.hex())
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)
        except Exception as err:
            self._last_error = str(err)
            self._publish_state()
            raise HomeAssistantError(f"GATT write failed: {err}") from err

    def _prime_from_last_service_info(self) -> None:
        """Prime RSSI and name from the latest discovery cache."""
        service_info = bluetooth.async_last_service_info(
            self.hass,
            self.address,
            connectable=True,
        )
        if service_info is None:
            return
        self._update_discovery_state(service_info)

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle a bluetooth advertisement update."""
        del change
        self._update_discovery_state(service_info)
        self._wake_event.set()
        self._publish_state()

    @callback
    def _update_discovery_state(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Update runtime state from discovery information."""
        self._rssi = service_info.rssi
        self._last_seen = datetime.now(UTC)
        discovered_name = service_info.name or service_info.device.name or self.name
        if discovered_name:
            self.name = discovered_name

    def _handle_live_data(self, _characteristic: Any, data: bytearray) -> None:
        """Handle a live notify frame."""
        frame = bytes(data)
        try:
            status = AC500Status.from_frame(frame)
        except ValueError:
            return

        self._last_status = status
        self._last_error = None
        self._live_event.set()
        self._publish_state()

    def _handle_ack(self, _characteristic: Any, data: bytearray) -> None:
        """Handle an ACK notify frame."""
        self._last_ack = bytes(data)
        self._ack_event.set()

    @callback
    def _publish_state(self) -> None:
        """Publish a fresh immutable snapshot."""
        self._update_callback(self.state)


class AC500Coordinator(DataUpdateCoordinator[AC500RuntimeState]):
    """Expose AC500 runtime state to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.default_name: str = entry.data.get(CONF_NAME, DEFAULT_NAME)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.address}",
        )

        self.manager = AC500ConnectionManager(
            hass,
            self.address,
            self.default_name,
            self._async_manager_published,
            reconnect_seconds=entry.options.get(CONF_RECONNECT_SECONDS, DEFAULT_RECONNECT_SECONDS),
            keepalive_seconds=entry.options.get(CONF_KEEPALIVE_SECONDS, DEFAULT_KEEPALIVE_SECONDS),
        )
        self.async_set_updated_data(self.manager.state)

    async def async_start(self) -> None:
        """Start the underlying manager."""
        await self.manager.async_start()

    async def async_stop(self) -> None:
        """Stop the underlying manager."""
        await self.manager.async_stop()

    async def _async_update_data(self) -> AC500RuntimeState:
        """Handle explicit refresh requests."""
        return await self.manager.async_force_refresh()

    async def async_set_power(self, enabled: bool) -> None:
        """Set power state."""
        await self.manager.async_set_power(enabled)

    async def async_set_uv(self, enabled: bool) -> None:
        """Set UV state."""
        await self.manager.async_set_uv(enabled)

    async def async_set_auto(self, enabled: bool) -> None:
        """Set auto mode."""
        await self.manager.async_set_auto(enabled)

    async def async_set_night(self, enabled: bool) -> None:
        """Set night mode."""
        await self.manager.async_set_night(enabled)

    async def async_set_buzzer(self, enabled: bool) -> None:
        """Set hidden buzzer mode."""
        await self.manager.async_set_buzzer(enabled)

    async def async_set_timer(self, option: str) -> None:
        """Set timer value."""
        if option not in TIMER_OPTIONS:
            raise HomeAssistantError(f"Unsupported timer option: {option}")
        await self.manager.async_set_timer(option)

    async def async_set_fan_level(self, level: str) -> None:
        """Set fan speed."""
        await self.manager.async_set_fan_level(level)

    async def async_run_pairing_handshake(self) -> None:
        """Run the AC500 onboarding handshake."""
        await self.manager.async_run_pairing_handshake()

    async def async_reconnect(self) -> None:
        """Force a reconnect."""
        await self.manager.async_reconnect()

    @callback
    def _async_manager_published(self, state: AC500RuntimeState) -> None:
        """Handle manager updates."""
        self.async_set_updated_data(state)


def is_ac500_service_info(service_info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    """Return True if the advertisement looks like an AC500."""
    name = (service_info.name or service_info.device.name or "").upper()
    if name == DEFAULT_NAME:
        return True

    service_uuids = {uuid.lower() for uuid in service_info.advertisement.service_uuids}
    if DISCOVERY_SERVICE_UUID in service_uuids:
        return True

    return MANUFACTURER_ID in service_info.manufacturer_data
