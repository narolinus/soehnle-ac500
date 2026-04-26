"""Constants for the Soehnle AC500 integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "soehnle_ac500"

MANUFACTURER = "Soehnle"
MODEL = "Airclean Connect 500"
DEVICE_NAME = "AC500"

SERVICE_UUID = "0000ffa0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ef01-0000-1000-8000-00805f9b34fb"
LIVE_DATA_CHAR_UUID = "0000ef02-0000-1000-8000-00805f9b34fb"
ACK_CHAR_UUID = "0000ef03-0000-1000-8000-00805f9b34fb"
HISTORY_CHAR_UUID = "0000ef04-0000-1000-8000-00805f9b34fb"

SCAN_INTERVAL = timedelta(minutes=5)
SESSION_TIMEOUT = 10.0
CONNECT_MAX_ATTEMPTS = 1
PAIR_TIMEOUT = 45.0
PAIR_REQUEST_INTERVAL = 2.0
STATUS_TIMEOUT = 3.0
COMMAND_TIMEOUT = 5.0

CONF_ADDRESS = "address"
CONF_NAME = "name"

PLATFORMS = [
    "binary_sensor",
    "button",
    "select",
    "sensor",
    "switch",
]

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTED = "connected"
STATE_STATUS_RECEIVED = "status_received"
STATE_PAIRING = "pairing"
STATE_PAIR_ACK = "pair_ack"
STATE_PAIRED = "paired"
STATE_PAIR_TIMEOUT = "pair_timeout"
STATE_STATUS_UNAVAILABLE = "status_unavailable"
STATE_COMMAND_SENT = "command_sent"
STATE_COMMAND_TIMEOUT = "command_timeout"
STATE_PARSE_FAILED = "live_parse_failed"
