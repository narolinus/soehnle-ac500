"""Config flow for the Soehnle AC500 integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback

from .const import DEFAULT_NAME, DOMAIN
from .coordinator import is_ac500_service_info

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_LOGGER = logging.getLogger(__name__)


def _normalize_address(address: str) -> str:
    """Normalize a Bluetooth address."""
    return address.replace("-", ":").upper()


def _is_valid_address(address: str) -> bool:
    """Return True if the address looks like a Bluetooth MAC."""
    return bool(_MAC_RE.fullmatch(address.strip()))


def _title_from_service_info(service_info: bluetooth.BluetoothServiceInfoBleak | None) -> str:
    """Build a config-entry title."""
    if service_info is None:
        return DEFAULT_NAME

    return service_info.name or service_info.device.name or DEFAULT_NAME


def _display_name(name: str, address: str) -> str:
    """Return a user-facing name with a short MAC suffix."""
    return f"{name} ({address[-8:]})"


def _discovered_ac500_devices(hass) -> dict[str, str]:
    """Return all currently discovered AC500 devices."""
    devices: dict[str, str] = {}
    for service_info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if not is_ac500_service_info(service_info):
            continue
        address = _normalize_address(service_info.address)
        source = getattr(service_info, "source", None)
        source_text = f" via {source}" if source else ""
        rssi_text = f" RSSI {service_info.rssi} dBm" if service_info.rssi is not None else ""
        devices[address] = (
            f"{_display_name(_title_from_service_info(service_info), address)}"
            f"{source_text}{rssi_text} - {address}"
        )
    return dict(sorted(devices.items(), key=lambda item: item[1]))


class SoehnleAC500ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Soehnle AC500.

    This config flow does NOT open a BLE connection to the device.
    It only checks that the device is visible in the Bluetooth scanner
    and creates the config entry.  The coordinator handles all BLE
    operations once the entry is loaded. If the purifier still needs
    onboarding, use the created Pair button afterwards.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_name = DEFAULT_NAME
        self._address: str | None = None
        self._source: str | None = None
        self._rssi: int | None = None

    @callback
    def _set_target(
        self,
        address: str,
        name: str,
        source: str | None = None,
        rssi: int | None = None,
    ) -> None:
        """Store the current target device."""
        self._address = _normalize_address(address)
        self._discovered_name = _display_name(name or DEFAULT_NAME, self._address)
        self._source = source
        self._rssi = rssi

    @callback
    def _details_text(self) -> str:
        """Build a short diagnostic text for the selected device."""
        parts: list[str] = []
        if self._source:
            parts.append(f"Scanner: {self._source}")
        if self._rssi is not None:
            parts.append(f"RSSI: {self._rssi} dBm")
        return " | ".join(parts) if parts else "Scanner/RSSI unknown"

    # ----- Auto-discovery via HA Bluetooth -----

    async def async_step_bluetooth(
        self,
        discovery_info: bluetooth.BluetoothServiceInfoBleak,
    ):
        """Handle bluetooth discovery."""
        address = _normalize_address(discovery_info.address)
        name = _title_from_service_info(discovery_info)
        self._set_target(
            address,
            name,
            getattr(discovery_info, "source", None),
            discovery_info.rssi,
        )
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured(
            updates={
                CONF_ADDRESS: address,
                CONF_NAME: self._discovered_name,
            }
        )

        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "address": address,
        }
        return await self.async_step_confirm_bluetooth()

    async def async_step_confirm_bluetooth(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Confirm a discovered device and create the entry."""
        if user_input is not None:
            # No BLE connection needed — just create the entry.
            return self._create_entry()

        return self.async_show_form(
            step_id="confirm_bluetooth",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._discovered_name,
                "address": self._address or "",
                "details": self._details_text(),
            },
        )

    # ----- Manual setup -----

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle initial setup."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["scan", "manual"],
        )

    async def async_step_scan(self, user_input: dict[str, Any] | None = None):
        """Pick one of the currently visible devices."""
        errors: dict[str, str] = {}
        discovered = _discovered_ac500_devices(self.hass)

        if bluetooth.async_scanner_count(self.hass, connectable=True) == 0:
            errors["base"] = "no_bluetooth_scanner"
        elif not discovered:
            errors["base"] = "no_devices_found"

        if user_input is not None and not errors:
            address = _normalize_address(user_input[CONF_ADDRESS])
            service_info = bluetooth.async_last_service_info(
                self.hass,
                address,
                connectable=True,
            )
            self._set_target(
                address,
                _title_from_service_info(service_info),
                getattr(service_info, "source", None) if service_info else None,
                service_info.rssi if service_info else None,
            )
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured(
                updates={
                    CONF_ADDRESS: address,
                    CONF_NAME: self._discovered_name,
                }
            )
            return self._create_entry()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ADDRESS,
                    default=next(iter(discovered)) if discovered else "",
                ): vol.In(discovered or {"": "No visible AC500 devices"}),
            }
        )
        return self.async_show_form(
            step_id="scan",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        """Set up a device by entering its MAC address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_address = user_input[CONF_ADDRESS].strip()
            if not _is_valid_address(raw_address):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                address = _normalize_address(raw_address)
                service_info = bluetooth.async_last_service_info(
                    self.hass,
                    address,
                    connectable=True,
                )
                self._set_target(
                    address,
                    _title_from_service_info(service_info),
                    getattr(service_info, "source", None) if service_info else None,
                    service_info.rssi if service_info else None,
                )
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_ADDRESS: address,
                        CONF_NAME: self._discovered_name,
                    }
                )
                return self._create_entry()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )

    # ----- Entry creation -----

    def _create_entry(self):
        """Create the config entry without a BLE connection.

        The coordinator will handle the connection lifecycle. If the purifier
        still needs pairing, use the Pair button after setup and press the
        purifier's Bluetooth button.
        """
        _LOGGER.info(
            "AC500 %s: creating config entry — "
            "the coordinator will handle connection and runtime control",
            self._address,
        )
        return self.async_create_entry(
            title=self._discovered_name,
            data={
                CONF_ADDRESS: self._address,
                CONF_NAME: self._discovered_name,
            },
        )
