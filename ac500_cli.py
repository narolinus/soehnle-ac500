#!/usr/bin/env python3
"""Local Bluetooth control for the Soehnle Airclean Connect 500.

This script is intentionally small and reusable so the protocol helpers can
later move into a Home Assistant integration with minimal churn.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shlex
import sys
from dataclasses import asdict, dataclass
from typing import Any, Callable

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - exercised only when bleak is missing.
    BleakClient = None
    BleakScanner = None


DEVICE_NAME = "AC500"
WRITE_CHAR_UUID = "0000ef01-0000-1000-8000-00805f9b34fb"
LIVE_DATA_CHAR_UUID = "0000ef02-0000-1000-8000-00805f9b34fb"
ACK_CHAR_UUID = "0000ef03-0000-1000-8000-00805f9b34fb"
HISTORY_CHAR_UUID = "0000ef04-0000-1000-8000-00805f9b34fb"

FAN_LABELS = {
    0: "low",
    1: "medium",
    2: "high",
    3: "turbo",
}
TIMER_LABELS = {
    0: "off",
    2: "2h",
    4: "4h",
    8: "8h",
}
FAN_COMMANDS = {
    "low": (0x02, 0x00, 0x00),
    "medium": (0x02, 0x00, 0x01),
    "high": (0x02, 0x00, 0x02),
    "turbo": (0x02, 0x00, 0x03),
}
TIMER_COMMANDS = {
    "off": (0x04, 0x00, 0x00),
    "2": (0x04, 0x00, 0x02),
    "4": (0x04, 0x00, 0x04),
    "8": (0x04, 0x00, 0x08),
}
POWER_COMMANDS = {
    "off": (0x01, 0x00, 0x00),
    "on": (0x01, 0x00, 0x01),
}
AUTO_COMMANDS = {
    "off": (0x05, 0x00, 0x00),
    "on": (0x05, 0x00, 0x01),
}
UV_COMMANDS = {
    "off": (0x03, 0x00, 0x00),
    "on": (0x03, 0x00, 0x01),
}
NIGHT_COMMANDS = {
    "off": (0x06, 0x00, 0x00),
    "on": (0x06, 0x00, 0x01),
}
BUZZER_COMMANDS = {
    "off": (0x08, 0x00, 0x00),
    "on": (0x08, 0x00, 0x01),
}


def ensure_bleak() -> None:
    if BleakClient is None or BleakScanner is None:
        raise SystemExit(
            "The 'bleak' package is not installed. Run "
            "'python3 -m pip install -r requirements.txt' first."
        )


def parse_int(value: str) -> int:
    return int(value, 0)


def frame_checksum(length: int, payload: bytes) -> int:
    return (length + sum(payload)) & 0xFF


def build_frame(opcode: int, arg1: int = 0x00, arg2: int = 0x00, length: int = 0x03) -> bytes:
    payload = bytes([opcode, arg1, arg2])
    checksum = frame_checksum(length, payload)
    return bytes([0xAA, length, *payload, checksum, 0xEE])


def validate_frame(frame: bytes) -> None:
    if len(frame) < 6:
        raise ValueError(f"Frame too short: {frame.hex()}")
    if frame[0] != 0xAA or frame[-1] != 0xEE:
        raise ValueError(f"Invalid frame markers: {frame.hex()}")
    length = frame[1]
    payload = frame[2:-2]
    if len(payload) != length:
        raise ValueError(
            f"Length mismatch in frame {frame.hex()}: expected {length}, got {len(payload)}"
        )
    checksum = frame[-2]
    expected = frame_checksum(length, payload)
    if checksum != expected:
        raise ValueError(
            f"Checksum mismatch in frame {frame.hex()}: expected 0x{expected:02x}, got 0x{checksum:02x}"
        )


@dataclass(slots=True)
class AC500Status:
    fan_raw: int
    fan_label: str
    timer_raw: int
    timer_label: str
    flags_raw: int
    reserved_raw: int
    pm_raw: int
    pm25_ug_m3: float
    temperature_raw: int
    temperature_c: float
    temperature_aux_raw: int
    filter_raw: int
    filter_aux_raw: int
    filter_percent: float
    power_enabled: bool
    uv_enabled: bool
    auto_enabled: bool
    night_enabled: bool
    timer_enabled: bool
    buzzer_enabled: bool
    raw_frame_hex: str

    @classmethod
    def from_frame(cls, frame: bytes) -> "AC500Status":
        validate_frame(frame)
        if frame[2] != 0xA0 or frame[3] != 0x21:
            raise ValueError(f"Unexpected live-data frame: {frame.hex()}")

        fan_raw = frame[4]
        timer_raw = frame[5]
        flags_raw = frame[6]
        reserved_raw = frame[7]
        pm_raw = frame[8] | (frame[9] << 8)
        temperature_raw = frame[10]
        temperature_aux_raw = frame[11]
        filter_raw = frame[12] | (frame[13] << 8)
        filter_aux_raw = frame[14]

        return cls(
            fan_raw=fan_raw,
            fan_label=FAN_LABELS.get(fan_raw, f"unknown({fan_raw})"),
            timer_raw=timer_raw,
            timer_label=TIMER_LABELS.get(timer_raw, f"unknown({timer_raw})"),
            flags_raw=flags_raw,
            reserved_raw=reserved_raw,
            pm_raw=pm_raw,
            pm25_ug_m3=pm_raw / 10.0,
            temperature_raw=temperature_raw,
            temperature_c=temperature_raw / 10.0,
            temperature_aux_raw=temperature_aux_raw,
            filter_raw=filter_raw,
            filter_aux_raw=filter_aux_raw,
            filter_percent=(filter_raw / 4320.0) * 100.0,
            # Bit mapping derived from the captured live-status frames.
            power_enabled=bool(flags_raw & 0x01),
            uv_enabled=bool(flags_raw & 0x02),
            timer_enabled=bool(flags_raw & 0x04),
            buzzer_enabled=bool(flags_raw & 0x08),
            auto_enabled=bool(flags_raw & 0x20),
            night_enabled=bool(flags_raw & 0x40),
            raw_frame_hex=frame.hex(),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["notes"] = {
            "pm25_ug_m3": "Observed as raw/10 from live status notifications.",
            "temperature_c": "Observed as raw/10 from live status notifications.",
            "filter_raw": "Raw value maps to percent approximately as raw / 4320 * 100.",
        }
        return data


class AC500Client:
    def __init__(self, address: str, verbose: bool = False, pair_before_setup: bool = False):
        ensure_bleak()
        self.address = address
        self.verbose = verbose
        self.pair_before_setup = pair_before_setup
        self.client = BleakClient(
            address,
            pair=pair_before_setup,
            timeout=30.0 if pair_before_setup else 10.0,
        )
        self.last_status: AC500Status | None = None
        self.last_ack: bytes | None = None
        self.live_event = asyncio.Event()
        self.ack_event = asyncio.Event()
        self.history_packets: list[str] = []

    async def __aenter__(self) -> "AC500Client":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self.disconnect()
        except Exception:
            # Do not let cleanup hide the original write/connect failure.
            if exc_type is None:
                raise

    async def connect(self) -> None:
        await self.client.connect()
        await self.client.start_notify(LIVE_DATA_CHAR_UUID, self._handle_live_data)
        await self.client.start_notify(ACK_CHAR_UUID, self._handle_ack)

    async def disconnect(self) -> None:
        if self.client.is_connected:
            with contextlib.suppress(EOFError):
                await self.client.disconnect()

    async def pair(self) -> bool:
        pair_method = getattr(self.client, "pair", None)
        if not callable(pair_method):
            raise RuntimeError(
                "This bleak backend does not expose pairing. Pair the AC500 once with the OS first."
            )

        if self.verbose:
            print("Attempting BLE pairing/bonding", file=sys.stderr)

        await pair_method()
        await asyncio.sleep(0.5)
        # bleak 1.x returns None on success for Linux/BlueZ and several other backends.
        return True

    async def start_history_notifications(self) -> None:
        await self.client.start_notify(HISTORY_CHAR_UUID, self._handle_history)

    async def initialize(self) -> AC500Status | None:
        await self.send_frame(0xAF, 0x00, 0x01, expect_status=False)
        await asyncio.sleep(0.2)
        return await self.request_status()

    async def enter_control_mode(self) -> AC500Status | None:
        # The captured control session sends AF twice and then waits for the
        # periodic live notifications before issuing control commands.
        await self.send_frame(0xAF, 0x00, 0x01, expect_status=False)
        await asyncio.sleep(0.1)
        await self.send_frame(0xAF, 0x00, 0x01, expect_status=False)

        self.live_event.clear()
        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=2.0)
        except TimeoutError:
            return await self.request_status()
        finally:
            self.live_event.clear()

        await asyncio.sleep(0.2)
        return self.last_status

    async def request_status(self) -> AC500Status | None:
        self.live_event.clear()
        await self.send_frame(0xA2, 0x00, 0x03, expect_status=False)
        try:
            await asyncio.wait_for(self.live_event.wait(), timeout=3.0)
        except TimeoutError:
            return self.last_status
        return self.last_status

    async def wait_for_ack(
        self,
        predicate: Callable[[bytes], bool],
        *,
        timeout: float = 10.0,
    ) -> bytes | None:
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

    async def run_pairing_handshake(self, *, timeout: float = 20.0) -> bool:
        # The captured pairing sequence uses EF03 ("pairing notify") and performs:
        #   write A2 00 03
        #   wait for EF03 notify A2 00 02
        #   write A2 00 01
        expected_ack = build_frame(0xA2, 0x00, 0x02)
        self.last_ack = None
        self.ack_event.clear()

        if self.verbose:
            print("Waiting for AC500 pairing handshake; press the Bluetooth button on the purifier now", file=sys.stderr)

        await self.send_frame(0xA2, 0x00, 0x03, expect_status=False)
        ack = await self.wait_for_ack(lambda data: data == expected_ack, timeout=timeout)
        if ack != expected_ack:
            return False

        await asyncio.sleep(0.1)
        await self.send_frame(0xA2, 0x00, 0x01, expect_status=False)
        await asyncio.sleep(0.3)
        return True

    async def wait_for_status(
        self,
        predicate: Callable[[AC500Status], bool],
        *,
        timeout: float = 5.0,
        refresh_interval: float = 1.0,
    ) -> AC500Status | None:
        deadline = asyncio.get_running_loop().time() + timeout

        while True:
            if self.last_status is not None and predicate(self.last_status):
                return self.last_status

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self.last_status

            self.live_event.clear()
            try:
                await asyncio.wait_for(self.live_event.wait(), timeout=min(refresh_interval, remaining))
                continue
            except TimeoutError:
                await self.request_status()
            finally:
                self.live_event.clear()

    async def send_frame(
        self,
        opcode: int,
        arg1: int = 0x00,
        arg2: int = 0x00,
        *,
        expect_status: bool = True,
        require_change_from: str | None = None,
        wait_timeout: float = 3.0,
    ) -> AC500Status | None:
        frame = build_frame(opcode, arg1, arg2)
        if self.verbose:
            print(f"TX {frame.hex()}", file=sys.stderr)

        self.live_event.clear()
        try:
            await self.client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)
        except Exception as exc:
            raise RuntimeError(
                "Sending the BLE command failed. "
                "The purifier likely requires prior BLE pairing/bonding for control writes. "
                "Put the AC500 into pairing mode with the Bluetooth button and run "
                f"'python3 ac500_cli.py --address {self.address} pair'. "
                f"Original error while sending {frame.hex()}: {exc}"
            ) from exc
        if not expect_status:
            return self.last_status

        deadline = asyncio.get_running_loop().time() + wait_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return self.last_status

            try:
                await asyncio.wait_for(self.live_event.wait(), timeout=remaining)
            except TimeoutError:
                return self.last_status
            finally:
                self.live_event.clear()

            if require_change_from is None:
                return self.last_status

            if self.last_status is None:
                continue

            if self.last_status.raw_frame_hex != require_change_from:
                return self.last_status

    def _handle_live_data(self, _characteristic: Any, data: bytearray) -> None:
        frame = bytes(data)
        if self.verbose:
            print(f"RX live {frame.hex()}", file=sys.stderr)
        try:
            self.last_status = AC500Status.from_frame(frame)
        except ValueError:
            if self.verbose:
                print("Ignoring non-status live frame", file=sys.stderr)
            return
        self.live_event.set()

    def _handle_ack(self, _characteristic: Any, data: bytearray) -> None:
        self.last_ack = bytes(data)
        if self.verbose:
            print(f"RX ack  {self.last_ack.hex()}", file=sys.stderr)
        self.ack_event.set()

    def _handle_history(self, _characteristic: Any, data: bytearray) -> None:
        packet = bytes(data).hex()
        self.history_packets.append(packet)
        print(packet)


async def resolve_address(address: str | None, timeout: float) -> str:
    ensure_bleak()
    if address:
        return address

    devices = await BleakScanner.discover(timeout=timeout)
    matches = [device for device in devices if (device.name or "").upper() == DEVICE_NAME]
    if not matches:
        raise SystemExit(
            "No AC500 device found during scan. Pass --address explicitly if the device is not advertising."
        )
    if len(matches) > 1:
        addresses = ", ".join(device.address for device in matches)
        raise SystemExit(
            f"Multiple AC500 devices found ({addresses}). Pass --address explicitly."
        )
    return matches[0].address


def print_status(status: AC500Status, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
        return

    print(f"fan:          {status.fan_label} ({status.fan_raw})")
    print(f"timer:        {status.timer_label} ({status.timer_raw})")
    print(f"pm2.5:        {status.pm25_ug_m3:.1f} ug/m3")
    print(f"temperature:  {status.temperature_c:.1f} C")
    print(f"flags:        0x{status.flags_raw:02x}")
    print(f"power:        {'on' if status.power_enabled else 'off'}")
    print(f"uv:           {'on' if status.uv_enabled else 'off'}")
    print(f"auto:         {'on' if status.auto_enabled else 'off'}")
    print(f"night:        {'on' if status.night_enabled else 'off'}")
    print(f"timer_on:     {'yes' if status.timer_enabled else 'no'}")
    print(f"buzzer:       {'on' if status.buzzer_enabled else 'off'}")
    print(f"filter:       {status.filter_percent:.1f}% ({status.filter_raw})")
    print(f"frame:        {status.raw_frame_hex}")


def status_predicate_for_command(opcode: int, arg2: int) -> Callable[[AC500Status], bool] | None:
    if opcode == 0x01:
        return lambda status: status.power_enabled == bool(arg2)
    if opcode == 0x02:
        return lambda status: status.fan_raw == arg2 and not status.auto_enabled
    if opcode == 0x03:
        return lambda status: status.uv_enabled == bool(arg2)
    if opcode == 0x04:
        return lambda status: status.timer_raw == arg2
    if opcode == 0x05:
        return lambda status: status.auto_enabled == bool(arg2)
    if opcode == 0x06:
        return lambda status: status.night_enabled == bool(arg2)
    if opcode == 0x08:
        return lambda status: status.buzzer_enabled == bool(arg2)
    return None


async def command_scan(args: argparse.Namespace) -> None:
    ensure_bleak()
    devices = await BleakScanner.discover(timeout=args.timeout)
    for device in sorted(devices, key=lambda item: (item.name or "", item.address)):
        name = device.name or "-"
        marker = " <==" if name.upper() == DEVICE_NAME else ""
        print(f"{device.address:>17}  {name}{marker}")


async def command_status(args: argparse.Namespace) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=args.pair) as device:
        status = await device.initialize()
        if status is None:
            raise SystemExit("No status frame received.")
        print_status(status, args.json)


async def command_monitor(args: argparse.Namespace) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=args.pair) as device:
        status = await device.initialize()
        if status is not None:
            print_status(status, args.json)
            print()

        deadline = None if args.seconds == 0 else asyncio.get_running_loop().time() + args.seconds
        last_frame = status.raw_frame_hex if status else None
        while deadline is None or asyncio.get_running_loop().time() < deadline:
            device.live_event.clear()
            try:
                await asyncio.wait_for(device.live_event.wait(), timeout=1.5)
            except TimeoutError:
                await device.request_status()
                continue

            if device.last_status is None:
                continue
            if device.last_status.raw_frame_hex == last_frame:
                continue
            last_frame = device.last_status.raw_frame_hex
            print_status(device.last_status, args.json)
            print()


async def command_history_dump(args: argparse.Namespace) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=args.pair) as device:
        await device.start_history_notifications()
        await device.send_frame(0xAF, 0x00, 0x01, expect_status=False)
        await asyncio.sleep(args.seconds)


async def command_pair(args: argparse.Namespace) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=True) as device:
        print("paired:       yes if BlueZ completed the connect/pair sequence without error")
        print("handshake:    waiting for EF03 pairing notify; press the Bluetooth button on the purifier now")
        handshake_ok = await device.run_pairing_handshake(timeout=args.pair_timeout)
        if handshake_ok:
            print("handshake:    completed")
        else:
            print("handshake:    no EF03 pairing response received")
        status = await device.initialize()
        if status is not None:
            print_status(status, args.json)


async def command_send(args: argparse.Namespace, frame_factory: Callable[[argparse.Namespace], tuple[int, int, int]]) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    opcode, arg1, arg2 = frame_factory(args)
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=args.pair) as device:
        baseline = await device.enter_control_mode()
        baseline_frame = baseline.raw_frame_hex if baseline is not None else None
        await device.send_frame(opcode, arg1, arg2, expect_status=False)
        predicate = status_predicate_for_command(opcode, arg2)
        if predicate is None:
            status = await device.request_status()
        else:
            status = await device.wait_for_status(predicate, timeout=5.0)

        if args.hold_seconds > 0:
            await asyncio.sleep(args.hold_seconds)
            status = await device.request_status() or status

        if status is None:
            print(f"Sent frame: {build_frame(opcode, arg1, arg2).hex()}")
            return
        print_status(status, args.json)
        if baseline_frame is not None and status.raw_frame_hex == baseline_frame:
            print("note: state unchanged after command; ATT write succeeded, but the purifier did not apply it")


async def command_power(args: argparse.Namespace) -> None:
    await command_send(args, lambda ns: POWER_COMMANDS[ns.state])


async def command_buzzer(args: argparse.Namespace) -> None:
    await command_send(args, lambda ns: BUZZER_COMMANDS[ns.state])


async def command_fan(args: argparse.Namespace) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    target_frame = FAN_COMMANDS[args.level]
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=args.pair) as device:
        baseline = await device.enter_control_mode()
        current = baseline

        # The captures always leave auto mode first before switching to a fixed speed.
        if current is not None and current.auto_enabled:
            await device.send_frame(*AUTO_COMMANDS["off"], expect_status=False)
            current = await device.wait_for_status(
                lambda status: not status.auto_enabled,
                timeout=5.0,
            )
            if current is not None:
                print("note: auto mode was active; sent auto off before fan change")

        reference = current.raw_frame_hex if current is not None else None
        await device.send_frame(*target_frame, expect_status=False)
        status = await device.wait_for_status(
            lambda result: result.fan_raw == target_frame[2] and not result.auto_enabled,
            timeout=5.0,
        )

        if args.hold_seconds > 0:
            await asyncio.sleep(args.hold_seconds)
            status = await device.request_status() or status

        if status is None:
            print(f"Sent frame: {build_frame(*target_frame).hex()}")
            return
        print_status(status, args.json)
        if reference is not None and status.raw_frame_hex == reference:
            print("note: state unchanged after command; ATT write succeeded, but the purifier did not apply it")


async def command_session(args: argparse.Namespace) -> None:
    address = await resolve_address(args.address, args.scan_timeout)
    async with AC500Client(address, verbose=args.verbose, pair_before_setup=args.pair) as device:
        status = await device.enter_control_mode()
        if status is not None:
            print_status(status, args.json)
            print()

        print("session:      connected")
        print("commands:     status | power on/off | fan low/medium/high/turbo | timer off/2/4/8 | uv on/off | night on/off | auto on/off | buzzer on/off | quit")

        while True:
            try:
                line = input("ac500> ").strip()
            except EOFError:
                print()
                return

            if not line:
                continue
            if line in {"quit", "exit"}:
                return

            try:
                parts = shlex.split(line)
            except ValueError as exc:
                print(f"error:        {exc}")
                continue

            try:
                if parts == ["status"]:
                    status = await device.request_status()
                elif len(parts) == 2 and parts[0] == "power" and parts[1] in POWER_COMMANDS:
                    await device.send_frame(*POWER_COMMANDS[parts[1]], expect_status=False)
                    status = await device.wait_for_status(
                        lambda result, expected=(parts[1] == "on"): result.power_enabled == expected
                    )
                elif len(parts) == 2 and parts[0] == "uv" and parts[1] in UV_COMMANDS:
                    await device.send_frame(*UV_COMMANDS[parts[1]], expect_status=False)
                    status = await device.wait_for_status(
                        lambda result, expected=(parts[1] == "on"): result.uv_enabled == expected
                    )
                elif len(parts) == 2 and parts[0] == "night" and parts[1] in NIGHT_COMMANDS:
                    await device.send_frame(*NIGHT_COMMANDS[parts[1]], expect_status=False)
                    status = await device.wait_for_status(
                        lambda result, expected=(parts[1] == "on"): result.night_enabled == expected
                    )
                elif len(parts) == 2 and parts[0] == "auto" and parts[1] in AUTO_COMMANDS:
                    await device.send_frame(*AUTO_COMMANDS[parts[1]], expect_status=False)
                    status = await device.wait_for_status(
                        lambda result, expected=(parts[1] == "on"): result.auto_enabled == expected
                    )
                elif len(parts) == 2 and parts[0] == "buzzer" and parts[1] in BUZZER_COMMANDS:
                    await device.send_frame(*BUZZER_COMMANDS[parts[1]], expect_status=False)
                    status = await device.wait_for_status(
                        lambda result, expected=(parts[1] == "on"): result.buzzer_enabled == expected
                    )
                elif len(parts) == 2 and parts[0] == "timer" and parts[1] in TIMER_COMMANDS:
                    await device.send_frame(*TIMER_COMMANDS[parts[1]], expect_status=False)
                    target = TIMER_COMMANDS[parts[1]][2]
                    status = await device.wait_for_status(
                        lambda result, expected=target: result.timer_raw == expected
                    )
                elif len(parts) == 2 and parts[0] == "fan" and parts[1] in FAN_COMMANDS:
                    if device.last_status is not None and device.last_status.auto_enabled:
                        await device.send_frame(*AUTO_COMMANDS["off"], expect_status=False)
                        await device.wait_for_status(lambda result: not result.auto_enabled)
                        print("note:         auto off before manual fan change")
                    target = FAN_COMMANDS[parts[1]][2]
                    await device.send_frame(*FAN_COMMANDS[parts[1]], expect_status=False)
                    status = await device.wait_for_status(
                        lambda result, expected=target: result.fan_raw == expected and not result.auto_enabled
                    )
                else:
                    print("error:        unsupported command")
                    continue
            except RuntimeError as exc:
                print(f"error:        {exc}")
                continue

            if status is None:
                print("error:        no status frame received")
                continue

            print_status(status, args.json)
            print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="Bluetooth MAC address of the purifier.")
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=5.0,
        help="Scan timeout in seconds when auto-discovering the device.",
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help="Attempt BLE pairing/bonding right after connecting.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON where possible.")
    parser.add_argument("--verbose", action="store_true", help="Print raw BLE frames to stderr.")
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0.0,
        help="After a write command, keep the BLE session open for N seconds before disconnecting.",
    )
    parser.add_argument(
        "--pair-timeout",
        type=float,
        default=20.0,
        help="Seconds to wait for the AC500 pairing handshake response on EF03.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan nearby BLE devices.")
    scan.add_argument("--timeout", type=float, default=5.0, help="Scan duration in seconds.")
    scan.set_defaults(func=command_scan)

    status = subparsers.add_parser("status", help="Fetch the current live status.")
    status.set_defaults(func=command_status)

    pair = subparsers.add_parser(
        "pair",
        help="Pair/bond with the purifier. Put the device into pairing mode first.",
    )
    pair.set_defaults(func=command_pair)

    monitor = subparsers.add_parser("monitor", help="Continuously print live status updates.")
    monitor.add_argument(
        "--seconds",
        type=float,
        default=0,
        help="Stop after N seconds. Use 0 to run until interrupted.",
    )
    monitor.set_defaults(func=command_monitor)

    session = subparsers.add_parser(
        "session",
        help="Keep one control connection open and accept commands interactively.",
    )
    session.set_defaults(func=command_session)

    history = subparsers.add_parser(
        "history-dump",
        help="Dump raw packets from the historic-data characteristic for reverse engineering.",
    )
    history.add_argument("--seconds", type=float, default=5.0, help="Capture duration in seconds.")
    history.set_defaults(func=command_history_dump)

    power = subparsers.add_parser("power", help="Turn the purifier on or off.")
    power.add_argument("state", choices=["on", "off"])
    power.set_defaults(func=command_power)

    fan = subparsers.add_parser("fan", help="Set the fan speed.")
    fan.add_argument("level", choices=sorted(FAN_COMMANDS), help="Target fan level.")
    fan.set_defaults(func=command_fan)

    timer = subparsers.add_parser("timer", help="Set the timer.")
    timer.add_argument("hours", choices=["off", "2", "4", "8"], help="Target timer value.")
    timer.set_defaults(func=lambda args: command_send(args, lambda ns: TIMER_COMMANDS[ns.hours]))

    uv = subparsers.add_parser("uv", help="Toggle UV-C.")
    uv.add_argument("state", choices=["on", "off"])
    uv.set_defaults(func=lambda args: command_send(args, lambda ns: UV_COMMANDS[ns.state]))

    night = subparsers.add_parser(
        "night",
        help="Toggle night mode.",
    )
    night.add_argument("state", choices=["on", "off"])
    night.set_defaults(func=lambda args: command_send(args, lambda ns: NIGHT_COMMANDS[ns.state]))

    auto = subparsers.add_parser(
        "auto",
        help="Toggle automatic mode.",
    )
    auto.add_argument("state", choices=["on", "off"])
    auto.set_defaults(func=lambda args: command_send(args, lambda ns: AUTO_COMMANDS[ns.state]))

    buzzer = subparsers.add_parser(
        "buzzer",
        help="Toggle the hidden beeper/buzzer setting.",
    )
    buzzer.add_argument("state", choices=["on", "off"])
    buzzer.set_defaults(func=command_buzzer)

    raw = subparsers.add_parser("raw", help="Send a raw protocol frame.")
    raw.add_argument("opcode", type=parse_int)
    raw.add_argument("arg1", type=parse_int, nargs="?", default=0)
    raw.add_argument("arg2", type=parse_int, nargs="?", default=0)
    raw.set_defaults(func=lambda args: command_send(args, lambda ns: (ns.opcode, ns.arg1, ns.arg2)))

    decode = subparsers.add_parser("decode-frame", help="Decode one captured live-data frame without BLE.")
    decode.add_argument("frame_hex", help="Hex-encoded frame, for example aa0da02100008b000a00cb10e0045375ee")
    decode.set_defaults(func=command_decode_frame)

    return parser


async def command_decode_frame(args: argparse.Namespace) -> None:
    frame = bytes.fromhex(args.frame_hex)
    status = AC500Status.from_frame(frame)
    print_status(status, args.json)


async def async_main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    await args.func(args)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
