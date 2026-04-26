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
    CONNECT_MAX_ATTEMPTS,
    DEVICE_NAME,
    LIVE_DATA_CHAR_UUID,
    PAIR_CONNECT_MAX_ATTEMPTS,
    PAIR_REQUEST_INTERVAL,
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
    STATE_STATUS_UNAVAILABLE,
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


class AC500BusyError(AC500CommunicationError):
    """Raised when another BLE operation is already running."""


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
        self._live_notify_started = False
        self._ack_notify_started = False

        self.last_status: AC500Status | None = None
        self.last_ack: bytes | None = None
        self.last_error: str | None = None
        self.state = STATE_DISCONNECTED
        self.connected = False
        self.rssi: int | None = None

    async def async_update(self) -> AC500Status | None:
        """Fetch a fresh status frame in a short BLE session."""
        async with self._lock:
            opened_here = not self._is_connected
            _LOGGER.warning(
                "%s refresh session start opened_here=%s connected=%s live_notify=%s ack_notify=%s",
                self.address,
                opened_here,
                self._is_connected,
                self._live_notify_started,
                self._ack_notify_started,
            )
            try:
                await self._connect(notify_live=True, notify_ack=False)
                status = await self._initialize_session()
                self.last_error = None
                _LOGGER.warning(
                    "%s refresh session done status=%s",
                    self.address,
                    status.raw_frame_hex if status else None,
                )
                return status
            finally:
                if opened_here:
                    await self._disconnect()

    async def async_pair(self) -> AC500Status | None:
        """Run BLE pairing, then the observed proprietary AC500 handshake."""
        async with self._lock:
            paired = False
            _LOGGER.warning(
                "%s pair session start connected=%s live_notify=%s ack_notify=%s",
                self.address,
                self._is_connected,
                self._live_notify_started,
                self._ack_notify_started,
            )
            try:
                await self._disconnect(force_state=True)
                await self._reset_bluez_if_live_notify_acquired()
                await asyncio.sleep(1.0)
                await self._connect(
                    notify_live=True,
                    notify_ack=True,
                    pair_before_connect=True,
                )
                self.state = STATE_PAIRING
                self._notify()

                pair_method = getattr(self._client, "pair", None)
                if callable(pair_method):
                    with contextlib.suppress(Exception):
                        await pair_method()

                expected = build_frame(0xA2, 0x00, 0x02)
                self.last_ack = None
                self._ack_event.clear()

                ack = await self._send_pair_requests_until_ack(expected)
                _LOGGER.warning(
                    "%s pair ack result=%s expected=%s",
                    self.address,
                    ack.hex() if ack else None,
                    expected.hex(),
                )
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
                paired = True
                self._notify()

                return await self._initialize_session()
            except AC500CommunicationError:
                raise
            finally:
                if not paired:
                    await self._disconnect()

    async def async_disconnect(self) -> None:
        """Disconnect from the purifier."""
        async with self._lock:
            await self._disconnect(force_state=True)

    async def async_connect_and_update(self) -> AC500Status | None:
        """Open a live session and keep it connected."""
        async with self._lock:
            if self._is_connected and not self._live_notify_started:
                await self._disconnect(force_state=True)
                await asyncio.sleep(1.0)
            await self._connect(notify_live=True, notify_ack=False)
            status = await self._initialize_session()
            self.last_error = None
            return status

    async def async_refresh(self) -> AC500Status | None:
        """Refresh status without disconnecting an existing session."""
        return await self.async_update()

    async def async_reconnect(self) -> AC500Status | None:
        """Disconnect and read status again."""
        async with self._lock:
            await self._disconnect(force_state=True)
            await asyncio.sleep(1.0)
            await self._connect(notify_live=True, notify_ack=False)
            return await self._initialize_session()

    async def async_reset_bluetooth_cache(self) -> None:
        """Remove the BlueZ device object to clear stale notify state."""
        async with self._lock:
            await self._disconnect(force_state=True)
            await self._remove_bluez_device()
            self.last_ack = None
            self.last_status = None
            self.last_error = "Bluetooth cache reset; wait for rediscovery and pair again"
            self._notify()

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
            opened_here = not self._is_connected
            _LOGGER.warning(
                "%s fan command start mode=%s opened_here=%s connected=%s",
                self.address,
                mode,
                opened_here,
                self._is_connected,
            )
            try:
                await self._connect(notify_live=True, notify_ack=False)
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
                if opened_here:
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

    @property
    def busy(self) -> bool:
        """Return true if a BLE operation is already running."""
        return self._lock.locked()

    async def _run_command(
        self,
        opcode: int,
        arg1: int,
        arg2: int,
        predicate: Callable[[AC500Status], bool],
    ) -> AC500Status | None:
        """Run one control-mode command and refresh status."""
        async with self._lock:
            opened_here = not self._is_connected
            _LOGGER.warning(
                "%s command start opcode=0x%02x arg1=0x%02x arg2=0x%02x opened_here=%s connected=%s",
                self.address,
                opcode,
                arg1,
                arg2,
                opened_here,
                self._is_connected,
            )
            try:
                await self._connect(notify_live=True, notify_ack=False)
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
                if opened_here:
                    await self._disconnect()

    async def _connect(
        self,
        *,
        notify_live: bool,
        notify_ack: bool,
        pair_before_connect: bool = False,
    ) -> None:
        """Open a BLE connection and subscribe to the requested notifications."""
        _LOGGER.warning(
            "%s connect requested notify_live=%s notify_ack=%s pair_before_connect=%s connected=%s",
            self.address,
            notify_live,
            notify_ack,
            pair_before_connect,
            self._is_connected,
        )
        if self._is_connected:
            if notify_live:
                await self._start_status_notifications()
            if notify_ack:
                await self._start_notify(ACK_CHAR_UUID, self._handle_ack)
            return

        ble_device = async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )
        if ble_device is None:
            _LOGGER.warning("%s no connectable bluetooth device found in HA cache", self.address)
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
                max_attempts=(
                    PAIR_CONNECT_MAX_ATTEMPTS
                    if pair_before_connect
                    else CONNECT_MAX_ATTEMPTS
                ),
                use_services_cache=False,
                timeout=30.0 if pair_before_connect else SESSION_TIMEOUT,
                pair=pair_before_connect,
            )
            if notify_live:
                await self._start_status_notifications()
            if notify_ack:
                await self._start_notify(ACK_CHAR_UUID, self._handle_ack)
        except (
            BleakError,
            TimeoutError,
            asyncio.TimeoutError,
            AC500CommunicationError,
        ) as err:
            if self._client is not None and self._client.is_connected:
                with contextlib.suppress(Exception):
                    await self._client.disconnect()
            self._client = None
            self._live_notify_started = False
            self._ack_notify_started = False
            if self.state != STATE_STATUS_UNAVAILABLE:
                self.last_error = str(err)
                self.state = STATE_DISCONNECTED
            self.connected = False
            if isinstance(err, AC500CommunicationError):
                _LOGGER.warning("%s connect failed: %s", self.address, err)
            else:
                _LOGGER.exception("%s connect failed: %s", self.address, err)
            self._notify()
            raise AC500CommunicationError(str(err)) from err

        self.connected = True
        self.state = STATE_CONNECTED
        _LOGGER.warning("%s connect done rssi=%s", self.address, self.rssi)
        self._notify()

    async def _start_status_notifications(self) -> None:
        """Start live status notifications on EF02."""
        try:
            await self._start_notify(LIVE_DATA_CHAR_UUID, self._handle_live_data)
        except AC500CommunicationError as err:
            self.state = STATE_STATUS_UNAVAILABLE
            self.last_error = (
                "Live status channel EF02 is unavailable. "
                f"BlueZ reported: {err}"
            )
            self._notify()
            raise

    async def _disconnect(self, *, force_state: bool = False) -> None:
        """Disconnect from the purifier."""
        _LOGGER.warning(
            "%s disconnect requested force_state=%s connected=%s live_notify=%s ack_notify=%s",
            self.address,
            force_state,
            self._is_connected,
            self._live_notify_started,
            self._ack_notify_started,
        )
        client = self._client
        if client is not None and client.is_connected:
            await self._stop_notify(ACK_CHAR_UUID)
            await self._stop_notify(LIVE_DATA_CHAR_UUID)
            with contextlib.suppress(Exception):
                await client.disconnect()

        self._client = None
        self._live_notify_started = False
        self._ack_notify_started = False
        self.connected = False
        if force_state or self.state == STATE_CONNECTED:
            self.state = STATE_DISCONNECTED
        self._notify()

    async def _remove_bluez_device(self) -> None:
        """Remove this device from BlueZ via D-Bus."""
        try:
            from dbus_fast import BusType, Message, MessageType
            from dbus_fast.aio import MessageBus
        except ImportError as err:
            raise AC500CommunicationError("dbus-fast is not available") from err

        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            objects_reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
            )
            if objects_reply.message_type == MessageType.ERROR:
                raise AC500CommunicationError(str(objects_reply.body[0]))

            device_path = self._bluez_device_path(self.address)
            adapter_path = "/org/bluez/hci0"

            for path, interfaces in objects_reply.body[0].items():
                device = interfaces.get("org.bluez.Device1")
                if device is None:
                    continue
                address = self._dbus_value(device.get("Address"))
                if str(address).upper() != self.address.upper():
                    continue
                device_path = path
                adapter_path = str(
                    self._dbus_value(device.get("Adapter")) or adapter_path
                )
                break

            _LOGGER.warning(
                "%s removing BlueZ device path=%s adapter=%s",
                self.address,
                device_path,
                adapter_path,
            )
            remove_reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path=adapter_path,
                    interface="org.bluez.Adapter1",
                    member="RemoveDevice",
                    signature="o",
                    body=[device_path],
                )
            )
            if remove_reply.message_type == MessageType.ERROR:
                raise AC500CommunicationError(str(remove_reply.body[0]))
        except AC500CommunicationError:
            raise
        except Exception as err:
            raise AC500CommunicationError(f"Could not reset BlueZ device: {err}") from err
        finally:
            if bus is not None:
                bus.disconnect()

    async def _reset_bluez_if_live_notify_acquired(self) -> None:
        """Reset BlueZ if EF02 is stuck in AcquireNotify state."""
        try:
            acquired = await self._bluez_live_notify_acquired()
        except AC500CommunicationError as err:
            _LOGGER.warning(
                "%s could not inspect BlueZ notify state: %s",
                self.address,
                err,
            )
            return

        if not acquired:
            return

        _LOGGER.warning(
            "%s BlueZ reports EF02 NotifyAcquired=True; resetting device cache before pairing",
            self.address,
        )
        self.last_error = (
            "BlueZ had a stale EF02 notification acquisition; reset Bluetooth cache "
            "before pairing."
        )
        self.state = STATE_DISCONNECTED
        self._notify()
        await self._remove_bluez_device()
        await asyncio.sleep(2.0)

    async def _bluez_live_notify_acquired(self) -> bool:
        """Return true if BlueZ says EF02 notify is acquired by another client."""
        try:
            from dbus_fast import BusType, Message, MessageType
            from dbus_fast.aio import MessageBus
        except ImportError as err:
            raise AC500CommunicationError("dbus-fast is not available") from err

        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
            )
            if reply.message_type == MessageType.ERROR:
                raise AC500CommunicationError(str(reply.body[0]))

            device_path = self._find_bluez_device_path(reply.body[0])
            if device_path is None:
                return False

            for _path, interfaces in reply.body[0].items():
                characteristic = interfaces.get("org.bluez.GattCharacteristic1")
                if characteristic is None:
                    continue

                uuid = str(self._dbus_value(characteristic.get("UUID"))).lower()
                service = str(self._dbus_value(characteristic.get("Service")))
                if uuid != LIVE_DATA_CHAR_UUID or not service.startswith(device_path):
                    continue

                return bool(self._dbus_value(characteristic.get("NotifyAcquired")))
        except AC500CommunicationError:
            raise
        except Exception as err:
            raise AC500CommunicationError(
                f"Could not inspect BlueZ notify state: {err}"
            ) from err
        finally:
            if bus is not None:
                bus.disconnect()

        return False

    def _find_bluez_device_path(self, objects: dict) -> str | None:
        """Find this device's BlueZ object path in managed objects."""
        for path, interfaces in objects.items():
            device = interfaces.get("org.bluez.Device1")
            if device is None:
                continue
            address = self._dbus_value(device.get("Address"))
            if str(address).upper() == self.address.upper():
                return str(path)
        return None

    @staticmethod
    def _dbus_value(value):
        """Unwrap dbus-fast Variant values."""
        return getattr(value, "value", value)

    @staticmethod
    def _bluez_device_path(address: str) -> str:
        """Return the conventional BlueZ object path for a BLE address."""
        return f"/org/bluez/hci0/dev_{address.replace(':', '_').upper()}"

    @property
    def _is_connected(self) -> bool:
        """Return true if a BLE connection is open."""
        return self._client is not None and self._client.is_connected

    async def _start_notify(
        self,
        characteristic_uuid: str,
        callback: Callable[[BleakGATTCharacteristic, bytearray], None],
    ) -> None:
        """Start one notification subscription if it is not already active."""
        if self._client is None or not self._client.is_connected:
            raise AC500CommunicationError("BLE client is not connected")

        if characteristic_uuid == LIVE_DATA_CHAR_UUID:
            if self._live_notify_started:
                return
            label = "live"
        elif characteristic_uuid == ACK_CHAR_UUID:
            if self._ack_notify_started:
                return
            label = "ack"
        else:
            label = characteristic_uuid

        try:
            _LOGGER.warning("%s start_notify %s via StartNotify", self.address, label)
            await self._client.start_notify(
                characteristic_uuid,
                callback,
                bluez={"use_start_notify": True},
            )
        except Exception as err:
            self.last_error = f"Could not enable {label} notifications: {err}"
            _LOGGER.warning("%s start_notify %s failed: %s", self.address, label, err)
            raise AC500CommunicationError(self.last_error) from err

        self._mark_notify_started(characteristic_uuid)
        _LOGGER.warning("%s start_notify %s done", self.address, label)

    def _mark_notify_started(self, characteristic_uuid: str) -> None:
        """Mark one notification subscription as active."""
        if characteristic_uuid == LIVE_DATA_CHAR_UUID:
            self._live_notify_started = True
        elif characteristic_uuid == ACK_CHAR_UUID:
            self._ack_notify_started = True

    async def _stop_notify(self, characteristic_uuid: str) -> None:
        """Stop one notification subscription if it is active."""
        if self._client is None or not self._client.is_connected:
            return

        if characteristic_uuid == LIVE_DATA_CHAR_UUID:
            if not self._live_notify_started:
                return
            self._live_notify_started = False
        elif characteristic_uuid == ACK_CHAR_UUID:
            if not self._ack_notify_started:
                return
            self._ack_notify_started = False

        with contextlib.suppress(Exception):
            _LOGGER.warning("%s stop_notify %s", self.address, characteristic_uuid)
            await self._client.stop_notify(characteristic_uuid)

    async def _initialize_session(self) -> AC500Status | None:
        """Run the lightweight status session from the working implementations."""
        _LOGGER.warning("%s initialize session", self.address)
        await self._write_command(0xAF, 0x00, 0x01)
        await asyncio.sleep(0.2)
        return await self._request_status()

    async def _enter_control_mode(self) -> AC500Status | None:
        """Enter the observed control mode before sending commands."""
        _LOGGER.warning("%s enter control mode", self.address)
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
        _LOGGER.warning("%s request status", self.address)
        self._live_event.clear()
        await self._write_command(0xA2, 0x00, 0x03)
        try:
            await asyncio.wait_for(self._live_event.wait(), timeout=STATUS_TIMEOUT)
        except TimeoutError:
            if self.last_status is None:
                self.last_error = "No live status frame received from EF02"
                self.state = STATE_STATUS_UNAVAILABLE
                self._notify()
            return self.last_status
        finally:
            self._live_event.clear()
        return self.last_status

    async def _write_command(self, opcode: int, arg1: int = 0, arg2: int = 0) -> None:
        """Write one framed command to EF01."""
        if self._client is None or not self._client.is_connected:
            raise AC500CommunicationError("BLE client is not connected")

        frame = build_frame(opcode, arg1, arg2)
        _LOGGER.warning("%s TX %s", self.address, frame.hex())
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

    async def _send_pair_requests_until_ack(self, expected: bytes) -> bytes | None:
        """Send repeated pairing requests while waiting for the device button."""
        deadline = asyncio.get_running_loop().time() + PAIR_TIMEOUT
        attempt = 0

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0 or not self._is_connected:
                return self.last_ack

            attempt += 1
            self.last_ack = None
            self._ack_event.clear()
            _LOGGER.warning(
                "%s pair request attempt=%s remaining=%.1fs; press the device bluetooth button",
                self.address,
                attempt,
                remaining,
            )
            await self._write_command(0xA2, 0x00, 0x03)

            ack = await self._wait_for_ack(
                lambda data: data == expected,
                min(PAIR_REQUEST_INTERVAL, remaining),
            )
            if ack == expected:
                return ack

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
        _LOGGER.warning("%s RX live %s", self.address, frame.hex())
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
        _LOGGER.warning("%s RX ack %s", self.address, self.last_ack.hex())
        try:
            self.last_status = AC500Status.from_frame(self.last_ack)
        except ValueError:
            pass
        else:
            self._last_seen_status_counter += 1
            self.state = STATE_STATUS_RECEIVED
            self._live_event.set()

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
        self._live_notify_started = False
        self._ack_notify_started = False
        self._live_event.set()
        self._ack_event.set()
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
