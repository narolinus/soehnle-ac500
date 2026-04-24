"""Constants for the Soehnle AC500 integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "soehnle_ac500"
MANUFACTURER = "Soehnle"
MODEL = "Airclean Connect 500"
DEFAULT_NAME = "AC500"

CONF_RECONNECT_SECONDS = "reconnect_seconds"
CONF_KEEPALIVE_SECONDS = "keepalive_seconds"

DEFAULT_RECONNECT_SECONDS = 15
DEFAULT_KEEPALIVE_SECONDS = 12

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.FAN,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

FAN_LEVELS = ["low", "medium", "high", "turbo"]
FAN_LEVEL_TO_INDEX = {level: index for index, level in enumerate(FAN_LEVELS)}
FAN_INDEX_TO_LEVEL = {index: level for index, level in enumerate(FAN_LEVELS)}

TIMER_OPTIONS = ["off", "2h", "4h", "8h"]
TIMER_OPTION_TO_VALUE = {
    "off": 0,
    "2h": 2,
    "4h": 4,
    "8h": 8,
}
TIMER_VALUE_TO_OPTION = {value: option for option, value in TIMER_OPTION_TO_VALUE.items()}

POLLING_FALLBACK = timedelta(seconds=30)
