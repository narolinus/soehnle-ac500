"""Config flow for Soehnle AC500."""

from __future__ import annotations

import re
from typing import Any

from homeassistant import config_entries
import voluptuous as vol

from .const import CONF_ADDRESS, CONF_NAME, DEVICE_NAME, DOMAIN

ADDRESS_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _normalize_address(address: str) -> str:
    """Normalize a Bluetooth address."""
    return address.strip().upper()


def _is_ac500_name(name: str | None) -> bool:
    """Return true if a Bluetooth name looks like an AC500."""
    return (name or "").upper() == DEVICE_NAME


def _entry_title(address: str) -> str:
    """Return a config entry title that distinguishes multiple AC500 devices."""
    return f"{DEVICE_NAME} ({address})"


class SoehnleAC500ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Soehnle AC500."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_device: Any | None = None

    async def async_step_bluetooth(
        self,
        discovery_info: Any,
    ) -> dict[str, Any]:
        """Handle Bluetooth discovery."""
        if not _is_ac500_name(discovery_info.name):
            return self.async_abort(reason="not_supported")

        address = _normalize_address(discovery_info.address)
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._discovered_device = discovery_info
        self.context["title_placeholders"] = {
            CONF_NAME: discovery_info.name or DEVICE_NAME,
            CONF_ADDRESS: address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Confirm a discovered AC500."""
        if user_input is not None and self._discovered_device is not None:
            address = _normalize_address(self._discovered_device.address)
            name = self._discovered_device.name or DEVICE_NAME
            return self.async_create_entry(
                title=_entry_title(address),
                data={CONF_ADDRESS: address, CONF_NAME: name},
            )

        address = (
            _normalize_address(self._discovered_device.address)
            if self._discovered_device is not None
            else ""
        )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={CONF_ADDRESS: address},
        )

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Handle manual setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            name = user_input.get(CONF_NAME) or DEVICE_NAME

            if not ADDRESS_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(address),
                    data={CONF_ADDRESS: address, CONF_NAME: name},
                )

        discovered = [
            info
            for info in _discovered_bluetooth_devices(self.hass)
            if _is_ac500_name(info.name)
        ]
        options = {
            _normalize_address(info.address): (
                f"{info.name or DEVICE_NAME} ({_normalize_address(info.address)})"
            )
            for info in discovered
        }

        schema_fields: dict[Any, Any] = {}
        if options:
            schema_fields[
                vol.Required(CONF_ADDRESS, default=next(iter(options)))
            ] = vol.In(options)
        else:
            schema_fields[vol.Required(CONF_ADDRESS)] = str
        schema_fields[vol.Optional(CONF_NAME, default=DEVICE_NAME)] = str

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )


def _discovered_bluetooth_devices(hass: Any) -> list[Any]:
    """Return connectable Bluetooth discoveries."""
    from homeassistant.components.bluetooth import async_discovered_service_info

    return list(async_discovered_service_info(hass, connectable=True))
