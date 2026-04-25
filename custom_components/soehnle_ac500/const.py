"""Constants for the Soehnle AC500 integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "soehnle_ac500"
MANUFACTURER = "Soehnle"
MODEL = "Airclean Connect 500"
DEFAULT_NAME = "AC500"

UPDATE_INTERVAL = timedelta(seconds=30)

CONNECT_TIMEOUT = 30.0
PAIRING_TIMEOUT = 20.0
STATUS_TIMEOUT = 3.0
STATUS_WAIT_TIMEOUT = 5.0
CONTROL_ENTER_DELAY = 0.1
CONTROL_SETTLE_DELAY = 0.2

SESSION_IDLE = "idle"
SESSION_STATUS_POLL = "status_poll"
SESSION_CONTROL = "control_mode"
SESSION_PAIRING = "pairing"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.FAN,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

FAN_LEVELS = ["low", "medium", "high", "turbo"]
TIMER_OPTIONS = ["off", "2h", "4h", "8h"]
