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
    DOMAIN,
    LIVE_DATA_CHAR_UUID,
    LIVE_STATUS_STALE_AFTER,
    PAIR_REQUEST_INTERVAL,
    PAIR_TIMEOUT,
    RECONNECT_INITIAL_DELAY,
    RECONNECT_RETRY_DELAY,
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
        self._keep_connected = False
        self._intentional_disconnect = False
        self._connecting = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._last_status_seen_at = 0.0

        self.last_status: AC500Status | None = None
        self.last_ack: bytes | None = None
        self.last_error: str | None = None
        self.state = STATE_DISCONNECTED
        self.connected = False
        self.rssi: int | None = None

    @callback
    def async_start(self) -> None:
        """Start the ESPHome-like persistent BLE session in the background."""
        self._keep_connected = True
        self._schedule_reconnect(delay=RECONNECT_INITIAL_DELAY)

    async def async_update(self) -> AC500Status | None:
        """Fetch status without disturbing a healthy live BLE session."""
        async with self._lock:
            self._keep_connected = True
            opened_here = not self._is_connected
            _LOGGER.warning(
                "%s refresh session start opened_here=%s connected=%s live_notify=%s ack_notify=%s",
                self.address,
                opened_here,
                self._is_connected,
                self._live_notify_started,
                self._ack_notify_started,
            )
            await self._connect(notify_live=True, notify_ack=False)
            if self._status_is_fresh(LIVE_STATUS_STALE_AFTER):
                self.last_error = None
                return self.last_status

            if self.last_status is None:
                status = await self._initialize_session()
            else:
                status = await self._request_status()
            self.last_error = None
            _LOGGER.warning(
                "%s refresh session done status=%s",
                self.address,
                status.raw_frame_hex if status else None,
            )
            return status

    async def async_pair(self) -> AC500Status | None:
        """Run the observed proprietary AC500 pairing handshake."""
        async with self._lock:
            self._keep_connected = True
            already_connected = self._is_connected
            _LOGGER.warning(
                "%s pair session start connected=%s live_notify=%s ack_notify=%s",
                self.address,
                self._is_connected,
                self._live_notify_started,
                self._ack_notify_started,
            )
            try:
                if not already_connected:
                    await self._reset_bluez_if_live_notify_acquired()

                await self._connect(
                    notify_live=True,
                    notify_ack=True,
                )
                self.state = STATE_PAIRING
                self._notify()

                _LOGGER.warning(
                    "%s running AC500 EF03 handshake without BlueZ Pair()",
                    self.address,
                )

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
                self._notify()

                return await self._initialize_session()
            except AC500CommunicationError:
                raise
            finally:
                if not self._is_connected:
                    self._schedule_reconnect(delay=RECONNECT_INITIAL_DELAY)

    async def async_disconnect(self) -> None:
        """Disconnect from the purifier."""
        async with self._lock:
            self._keep_connected = False
            self._cancel_reconnect_task()
            self._cancel_keepalive_task()
            await self._disconnect(force_state=True)

    async def async_connect_and_update(self) -> AC500Status | None:
        """Open a live session and keep it connected."""
        async with self._lock:
            self._keep_connected = True
            await self._connect(notify_live=True, notify_ack=False)
            if self._status_is_fresh(LIVE_STATUS_STALE_AFTER):
                self.last_error = None
                return self.last_status
            status = await self._initialize_session()
            self.last_error = None
            return status

    async def async_refresh(self) -> AC500Status | None:
        """Refresh status without disconnecting an existing session."""
        async with self._lock:
            self._keep_connected = True
            await self._connect(notify_live=True, notify_ack=False)
            await self._enter_control_mode()
            status = await self._request_status()
            self.last_error = None
            return status

    async def async_reconnect(self) -> AC500Status | None:
        """Rebuild the live session explicitly."""
        async with self._lock:
            self._keep_connected = True
            await self._disconnect(force_state=True)
            await asyncio.sleep(0.5)
            await self._connect(notify_live=True, notify_ack=False)
            return await self._initialize_session()

    async def async_reset_bluetooth_cache(self) -> None:
        """Remove the BlueZ device object to clear stale notify state."""
        async with self._lock:
            self._keep_connected = False
            self._cancel_reconnect_task()
            self._cancel_keepalive_task()
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
            self._keep_connected = True
            opened_here = not self._is_connected
            _LOGGER.warning(
                "%s fan command start mode=%s opened_here=%s connected=%s",
                self.address,
                mode,
                opened_here,
                self._is_connected,
            )
            await self._connect(notify_live=True, notify_ack=False)
            await self._enter_control_mode()

            if self.last_status is not None and self.last_status.auto_enabled:
                wait_from = self._last_seen_status_counter
                await self._write_command(*AUTO_COMMANDS[False])
                await self._wait_for_status(
                    lambda status: not status.auto_enabled,
                    COMMAND_TIMEOUT,
                    since_counter=wait_from,
                )
                await asyncio.sleep(0.25)

            wait_from = self._last_seen_status_counter
            await self._write_command(0x02, 0x00, fan)
            status = await self._wait_for_status(
                lambda item: item.fan_raw == fan and not item.auto_enabled,
                COMMAND_TIMEOUT,
                since_counter=wait_from,
            )
            if status is None:
                self.state = STATE_COMMAND_TIMEOUT
                self.last_error = f"Fan mode {mode} was not confirmed by live status"
                self._notify()
                raise AC500CommunicationError(self.last_error)

            self.state = STATE_COMMAND_SENT
            self.last_error = None
            self._notify()
            return status

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
            self._keep_connected = False
            self._cancel_reconnect_task()
            self._cancel_keepalive_task()
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
            self._keep_connected = True
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
            await self._connect(notify_live=True, notify_ack=False)
            await self._enter_control_mode()
            wait_from = self._last_seen_status_counter
            await self._write_command(opcode, arg1, arg2)
            status = await self._wait_for_status(
                predicate,
                COMMAND_TIMEOUT,
                since_counter=wait_from,
            )
            if status is None:
                self.state = STATE_COMMAND_TIMEOUT
                self.last_error = (
                    f"Command 0x{opcode:02x} 0x{arg1:02x} 0x{arg2:02x} "
                    "was not confirmed by live status"
                )
                self._notify()
                raise AC500CommunicationError(self.last_error)

            self.state = STATE_COMMAND_SENT
            self.last_error = None
            self._notify()
            return status

    async def _connect(
        self,
        *,
        notify_live: bool,
        notify_ack: bool,
    ) -> None:
        """Open a BLE connection and subscribe to the requested notifications."""
        _LOGGER.warning(
            "%s connect requested notify_live=%s notify_ack=%s connected=%s",
            self.address,
            notify_live,
            notify_ack,
            self._is_connected,
        )
        if self._is_connected:
            await self._ensure_notifications(
                notify_live=notify_live,
                notify_ack=notify_ack,
            )
            return

        async with self._connect_lock():
            if self._is_connected:
                await self._ensure_notifications(
                    notify_live=notify_live,
                    notify_ack=notify_ack,
                )
                return

            ble_device = async_ble_device_from_address(
                self.hass,
                self.address,
                connectable=True,
            )
            if ble_device is None:
                _LOGGER.warning(
                    "%s no connectable bluetooth device found in HA cache",
                    self.address,
                )
                self.last_error = (
                    f"{self.name} is not currently visible to Home Assistant Bluetooth"
                )
                self.state = STATE_DISCONNECTED
                self.connected = False
                self._notify()
                raise AC500CommunicationError(
                    self.last_error
                )

            self.rssi = getattr(ble_device, "rssi", None)

            try:
                self._connecting = True
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
                    max_attempts=CONNECT_MAX_ATTEMPTS,
                    use_services_cache=False,
                    timeout=SESSION_TIMEOUT,
                )
                await self._ensure_notifications(
                    notify_live=notify_live,
                    notify_ack=notify_ack,
                )
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
                if self._keep_connected:
                    self._schedule_reconnect(delay=RECONNECT_RETRY_DELAY)
                raise AC500CommunicationError(str(err)) from err
            finally:
                self._connecting = False

        self.connected = True
        self.state = STATE_CONNECTED
        _LOGGER.warning("%s connect done rssi=%s", self.address, self.rssi)
        self._notify()

    async def _ensure_notifications(
        self,
        *,
        notify_live: bool,
        notify_ack: bool,
    ) -> None:
        """Subscribe to requested notifications on the current connection."""
        if notify_live:
            await self._start_status_notifications()
            self._keep_connected = True
            self._schedule_keepalive()
        if notify_ack:
            await self._start_notify(ACK_CHAR_UUID, self._handle_ack)
        self.connected = True

    def _connect_lock(self) -> asyncio.Lock:
        """Return a shared lock for adapter connection attempts."""
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        lock = domain_data.get("connect_lock")
        if lock is None:
            lock = asyncio.Lock()
            domain_data["connect_lock"] = lock
        return lock

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
            self._intentional_disconnect = True
            try:
                await self._stop_notify(ACK_CHAR_UUID)
                await self._stop_notify(LIVE_DATA_CHAR_UUID)
                with contextlib.suppress(Exception):
                    await client.disconnect()
            finally:
                self._intentional_disconnect = False

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

    def _status_is_fresh(self, max_age: float) -> bool:
        """Return true if the latest live frame is recent enough to reuse."""
        if self.last_status is None or self._last_status_seen_at <= 0:
            return False
        return self.hass.loop.time() - self._last_status_seen_at <= max_age

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
        self._live_event.clear()
        await self._write_command(0xAF, 0x00, 0x01)
        await asyncio.sleep(0.12)
        await self._write_command(0xAF, 0x00, 0x01)

        try:
            await asyncio.wait_for(self._live_event.wait(), timeout=2.0)
        except TimeoutError:
            _LOGGER.warning("%s no fresh live frame after control mode", self.address)
        finally:
            self._live_event.clear()

        if self._last_seen_status_counter == start_counter:
            _LOGGER.warning("%s stale live event after control mode", self.address)

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
            self._notify()
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
        *,
        since_counter: int | None = None,
    ) -> AC500Status | None:
        """Wait for a matching status, requesting status only if no live frame arrives."""
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        deadline = start_time + timeout
        start_counter = (
            self._last_seen_status_counter
            if since_counter is None
            else since_counter
        )
        status_requested = False

        while True:
            if (
                self.last_status is not None
                and predicate(self.last_status)
                and self._last_seen_status_counter != start_counter
            ):
                return self.last_status

            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                if (
                    self.last_status is not None
                    and predicate(self.last_status)
                    and self._last_seen_status_counter != start_counter
                ):
                    return self.last_status
                return None

            self._live_event.clear()
            try:
                await asyncio.wait_for(self._live_event.wait(), timeout=min(0.5, remaining))
            except TimeoutError:
                if (
                    self._last_seen_status_counter == start_counter
                    and not status_requested
                    and loop.time() - start_time >= 2.0
                ):
                    status_requested = True
                    _LOGGER.warning(
                        "%s no live frame after command; requesting status",
                        self.address,
                    )
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
        self._last_status_seen_at = self.hass.loop.time()
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
            self._last_status_seen_at = self.hass.loop.time()
            self.state = STATE_STATUS_RECEIVED
            self._live_event.set()

        if is_pair_ack(self.last_ack):
            self.state = STATE_PAIR_ACK
        self._ack_event.set()
        self._notify()

    @callback
    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Handle a BLE disconnect callback."""
        if self._client is None:
            _LOGGER.debug(
                "%s ignoring disconnect callback without active client connecting=%s",
                self.address,
                self._connecting,
            )
            return
        if self._client is not _client:
            _LOGGER.debug("%s ignoring stale disconnect callback", self.address)
            return
        _LOGGER.warning(
            "%s disconnected callback intentional=%s keep_connected=%s",
            self.address,
            self._intentional_disconnect,
            self._keep_connected,
        )
        self.connected = False
        self.state = STATE_DISCONNECTED
        self._client = None
        self._live_notify_started = False
        self._ack_notify_started = False
        self._live_event.set()
        self._ack_event.set()
        self._notify()
        if self._keep_connected and not self._intentional_disconnect:
            self._schedule_reconnect()

    def _cancel_reconnect_task(self) -> None:
        """Cancel a pending automatic reconnect."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None

    def _cancel_keepalive_task(self) -> None:
        """Cancel a pending live-session keepalive."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    @callback
    def _schedule_reconnect(self, delay: float = RECONNECT_INITIAL_DELAY) -> None:
        """Schedule ESPHome-like automatic reconnect after an unexpected drop."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(self._reconnect_loop(delay))

    async def _reconnect_loop(self, delay: float) -> None:
        """Reconnect the live BLE session while the integration wants it open."""
        while self._keep_connected:
            await asyncio.sleep(delay)
            if self._is_connected:
                return
            if self._lock.locked():
                delay = 5.0
                continue

            async with self._lock:
                if not self._keep_connected or self._is_connected:
                    return
                try:
                    _LOGGER.warning("%s automatic reconnect attempt", self.address)
                    await self._connect(notify_live=True, notify_ack=False)
                    await self._initialize_session()
                    self.last_error = None
                    return
                except AC500CommunicationError as err:
                    self.last_error = f"Automatic reconnect failed: {err}"
                    self.state = STATE_DISCONNECTED
                    self.connected = False
                    self._notify()
                    delay = RECONNECT_RETRY_DELAY

    @callback
    def _schedule_keepalive(self) -> None:
        """Schedule status keepalive while a live session should remain open."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = self.hass.async_create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Request status if live notifications stall before BlueZ disconnects."""
        while self._keep_connected:
            await asyncio.sleep(LIVE_STATUS_STALE_AFTER)
            if not self._is_connected or self._lock.locked():
                continue

            age = self.hass.loop.time() - self._last_status_seen_at
            if self._last_status_seen_at > 0 and age <= LIVE_STATUS_STALE_AFTER:
                continue

            async with self._lock:
                if not self._keep_connected or not self._is_connected:
                    continue
                age = self.hass.loop.time() - self._last_status_seen_at
                if self._last_status_seen_at > 0 and age <= LIVE_STATUS_STALE_AFTER:
                    continue
                try:
                    _LOGGER.warning(
                        "%s live status stalled for %.1fs; requesting keepalive status",
                        self.address,
                        age,
                    )
                    await self._request_status()
                except AC500CommunicationError as err:
                    self.last_error = f"Keepalive status request failed: {err}"
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
