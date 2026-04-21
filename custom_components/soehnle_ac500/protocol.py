"""Protocol helpers for the Soehnle AC500."""

from __future__ import annotations

from dataclasses import dataclass

from .const import FAN_INDEX_TO_LEVEL, TIMER_VALUE_TO_OPTION

DEVICE_NAME = "AC500"
WRITE_CHAR_UUID = "0000ef01-0000-1000-8000-00805f9b34fb"
LIVE_DATA_CHAR_UUID = "0000ef02-0000-1000-8000-00805f9b34fb"
ACK_CHAR_UUID = "0000ef03-0000-1000-8000-00805f9b34fb"
HISTORY_CHAR_UUID = "0000ef04-0000-1000-8000-00805f9b34fb"
DISCOVERY_SERVICE_UUID = "0000ffa0-0000-1000-8000-00805f9b34fb"
MANUFACTURER_ID = 0x07E0

POWER_COMMANDS = {
    "off": (0x01, 0x00, 0x00),
    "on": (0x01, 0x00, 0x01),
}
FAN_COMMANDS = {
    "low": (0x02, 0x00, 0x00),
    "medium": (0x02, 0x00, 0x01),
    "high": (0x02, 0x00, 0x02),
    "turbo": (0x02, 0x00, 0x03),
}
UV_COMMANDS = {
    "off": (0x03, 0x00, 0x00),
    "on": (0x03, 0x00, 0x01),
}
TIMER_COMMANDS = {
    "off": (0x04, 0x00, 0x00),
    "2h": (0x04, 0x00, 0x02),
    "4h": (0x04, 0x00, 0x04),
    "8h": (0x04, 0x00, 0x08),
}
AUTO_COMMANDS = {
    "off": (0x05, 0x00, 0x00),
    "on": (0x05, 0x00, 0x01),
}
NIGHT_COMMANDS = {
    "off": (0x06, 0x00, 0x00),
    "on": (0x06, 0x00, 0x01),
}
BUZZER_COMMANDS = {
    "off": (0x08, 0x00, 0x00),
    "on": (0x08, 0x00, 0x01),
}


def frame_checksum(length: int, payload: bytes) -> int:
    """Return the AC500 frame checksum."""
    return (length + sum(payload)) & 0xFF


def build_frame(opcode: int, arg1: int = 0x00, arg2: int = 0x00, length: int = 0x03) -> bytes:
    """Build a command frame."""
    payload = bytes([opcode, arg1, arg2])
    checksum = frame_checksum(length, payload)
    return bytes([0xAA, length, *payload, checksum, 0xEE])


def validate_frame(frame: bytes) -> None:
    """Validate a frame."""
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


@dataclass(slots=True, frozen=True)
class AC500Status:
    """Decoded AC500 live status."""

    fan_raw: int
    fan_label: str
    timer_raw: int
    timer_label: str
    flags_raw: int
    pm_raw: int
    pm25_ug_m3: float
    temperature_raw: int
    temperature_c: float
    filter_raw: int
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
        """Decode a live status frame."""
        validate_frame(frame)
        if frame[2] != 0xA0 or frame[3] != 0x21:
            raise ValueError(f"Unexpected live frame: {frame.hex()}")

        fan_raw = frame[4]
        timer_raw = frame[5]
        flags_raw = frame[6]
        pm_raw = frame[8] | (frame[9] << 8)
        temperature_raw = frame[10]
        filter_raw = frame[12] | (frame[13] << 8)

        return cls(
            fan_raw=fan_raw,
            fan_label=FAN_INDEX_TO_LEVEL.get(fan_raw, f"unknown_{fan_raw}"),
            timer_raw=timer_raw,
            timer_label=TIMER_VALUE_TO_OPTION.get(timer_raw, f"unknown_{timer_raw}"),
            flags_raw=flags_raw,
            pm_raw=pm_raw,
            pm25_ug_m3=pm_raw / 10.0,
            temperature_raw=temperature_raw,
            temperature_c=temperature_raw / 10.0,
            filter_raw=filter_raw,
            filter_percent=(filter_raw / 4320.0) * 100.0,
            power_enabled=bool(flags_raw & 0x01),
            uv_enabled=bool(flags_raw & 0x02),
            timer_enabled=bool(flags_raw & 0x04),
            buzzer_enabled=bool(flags_raw & 0x08),
            auto_enabled=bool(flags_raw & 0x20),
            night_enabled=bool(flags_raw & 0x40),
            raw_frame_hex=frame.hex(),
        )
