"""Protocol helpers for the Soehnle Airclean Connect 500."""

from __future__ import annotations

from dataclasses import dataclass

FRAME_START = 0xAA
FRAME_END = 0xEE
FRAME_LENGTH = 0x03
FILTER_RAW_FULL_SCALE = 4320.0

FAN_LABELS = {
    0: "Low",
    1: "Medium",
    2: "High",
    3: "Turbo",
}
FAN_VALUES = {label: raw for raw, label in FAN_LABELS.items()}

TIMER_LABELS = {
    0: "Off",
    2: "2 h",
    4: "4 h",
    8: "8 h",
}
TIMER_VALUES = {label: raw for raw, label in TIMER_LABELS.items()}

POWER_COMMANDS = {
    False: (0x01, 0x00, 0x00),
    True: (0x01, 0x00, 0x01),
}
UV_COMMANDS = {
    False: (0x03, 0x00, 0x00),
    True: (0x03, 0x00, 0x01),
}
AUTO_COMMANDS = {
    False: (0x05, 0x00, 0x00),
    True: (0x05, 0x00, 0x01),
}
NIGHT_COMMANDS = {
    False: (0x06, 0x00, 0x00),
    True: (0x06, 0x00, 0x01),
}
BUZZER_COMMANDS = {
    False: (0x08, 0x00, 0x00),
    True: (0x08, 0x00, 0x01),
}


def frame_checksum(length: int, payload: bytes) -> int:
    """Return the AC500 frame checksum."""
    return (length + sum(payload)) & 0xFF


def build_frame(opcode: int, arg1: int = 0x00, arg2: int = 0x00) -> bytes:
    """Build a write frame."""
    payload = bytes([opcode, arg1, arg2])
    return bytes(
        [
            FRAME_START,
            FRAME_LENGTH,
            *payload,
            frame_checksum(FRAME_LENGTH, payload),
            FRAME_END,
        ]
    )


def validate_frame(frame: bytes) -> None:
    """Validate a received AC500 frame."""
    if len(frame) < 6:
        raise ValueError(f"Frame too short: {frame.hex()}")
    if frame[0] != FRAME_START or frame[-1] != FRAME_END:
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
            f"Checksum mismatch in frame {frame.hex()}: "
            f"expected 0x{expected:02x}, got 0x{checksum:02x}"
        )


def is_pair_ack(frame: bytes) -> bool:
    """Return true if an EF03 notification is the observed pairing ack."""
    return frame == build_frame(0xA2, 0x00, 0x02)


@dataclass(slots=True, frozen=True)
class AC500Status:
    """Decoded live status from an AC500 notification."""

    frame_variant: int
    fan_raw: int
    timer_raw: int
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
    raw_frame_hex: str

    @classmethod
    def from_frame(cls, frame: bytes) -> "AC500Status":
        """Decode a live status frame."""
        validate_frame(frame)
        if frame[2] != 0xA0 or frame[3] not in (0x21, 0x22):
            raise ValueError(f"Unexpected live-data frame: {frame.hex()}")

        fan_raw = frame[4]
        timer_raw = frame[5]
        flags_raw = frame[6]

        if frame[3] == 0x21:
            reserved_raw = frame[7]
            pm_raw = frame[8] | (frame[9] << 8)
        else:
            pm_raw = (frame[7] << 8) | frame[8]
            reserved_raw = frame[9]

        temperature_raw = frame[10]
        filter_raw = frame[12] | (frame[13] << 8)

        return cls(
            frame_variant=frame[3],
            fan_raw=fan_raw,
            timer_raw=timer_raw,
            flags_raw=flags_raw,
            reserved_raw=reserved_raw,
            pm_raw=pm_raw,
            pm25_ug_m3=pm_raw / 10.0,
            temperature_raw=temperature_raw,
            temperature_c=temperature_raw / 10.0,
            temperature_aux_raw=frame[11],
            filter_raw=filter_raw,
            filter_aux_raw=frame[14],
            filter_percent=(filter_raw / FILTER_RAW_FULL_SCALE) * 100.0,
            raw_frame_hex=frame.hex(),
        )

    @property
    def fan_label(self) -> str | None:
        """Return the current fan label."""
        return FAN_LABELS.get(self.fan_raw)

    @property
    def timer_label(self) -> str | None:
        """Return the current timer label."""
        return TIMER_LABELS.get(self.timer_raw)

    @property
    def power_enabled(self) -> bool:
        """Return true if the purifier is powered on."""
        return bool(self.flags_raw & 0x01)

    @property
    def uv_enabled(self) -> bool:
        """Return true if UV-C is enabled."""
        return bool(self.flags_raw & 0x02)

    @property
    def timer_enabled(self) -> bool:
        """Return true if the timer is active."""
        return bool(self.flags_raw & 0x04)

    @property
    def buzzer_enabled(self) -> bool:
        """Return true if the buzzer is enabled."""
        return bool(self.flags_raw & 0x08)

    @property
    def auto_enabled(self) -> bool:
        """Return true if automatic mode is enabled."""
        return bool(self.flags_raw & 0x20)

    @property
    def night_enabled(self) -> bool:
        """Return true if night mode is enabled."""
        return bool(self.flags_raw & 0x40)
