"""Diagnostics support for the Daikin One integration.

Everything a user might paste into an issue goes through `async_redact_data`, and the
thermostat section is built from redaction-safe fields only: no id, no thermostat name,
no location name and never the access token (only the seconds left on it).
"""

from __future__ import annotations

from dataclasses import asdict
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.climate.const import HVACMode
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .api import ModeLimit, Thermostat, ThermostatState
from .api.const import API_BASE_URL
from .const import DOMAIN

if TYPE_CHECKING:
    from . import DaikinOneConfigEntry
    from .coordinator import DaikinOneCoordinator

__all__ = ["TO_REDACT", "async_get_config_entry_diagnostics"]

TO_REDACT: Final[set[str]] = {
    "email",
    "api_key",
    "integrator_token",
    "password",
    "access_token",
    "authorization",
    "cookie",
    "unique_id",
    "title",
    "id",
    "name",
    "location_name",
}

#: Mirrors the mode-limit filtering in `climate.py`; kept here so a diagnostics dump shows
#: which modes the climate entity actually offers for that thermostat.
_HVAC_MODES_BY_LIMIT: Final[dict[ModeLimit | None, list[HVACMode]]] = {
    ModeLimit.HEAT_ONLY: [HVACMode.OFF, HVACMode.HEAT],
    ModeLimit.COOL_ONLY: [HVACMode.OFF, HVACMode.COOL],
}
_ALL_HVAC_MODES: Final[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]


def _state_dict(state: ThermostatState) -> dict[str, Any]:
    """Dump the state, replacing enum members with their readable names."""
    return {key: value.name if isinstance(value, IntEnum) else value for key, value in asdict(state).items()}


def _thermostat_diagnostics(thermostat: Thermostat) -> dict[str, Any]:
    """Describe one thermostat without any identifying field."""
    state = thermostat.state
    return {
        "online": thermostat.online,
        "model": thermostat.summary.model,
        "firmware_version": thermostat.summary.firmware_version,
        "capabilities": {
            "mode_limit": state.mode_limit.name if state.mode_limit is not None else None,
            "em_heat_available": state.em_heat_available,
            "hvac_modes_exposed": [str(mode) for mode in _HVAC_MODES_BY_LIMIT.get(state.mode_limit, _ALL_HVAC_MODES)],
            "setpoint_minimum": state.setpoint_minimum,
            "setpoint_maximum": state.setpoint_maximum,
            "setpoint_delta": state.setpoint_delta,
        },
        "state": _state_dict(state),
    }


def _coordinator_diagnostics(coordinator: DaikinOneCoordinator) -> dict[str, Any]:
    """Poll scheduling, last-failure and thermostat details of a loaded entry."""
    interval = coordinator.update_interval
    expires_in = coordinator.client.auth.expires_in
    success_time = coordinator.last_update_success_time
    return {
        "base_interval_seconds": coordinator.base_interval,
        "current_update_interval_seconds": int(interval.total_seconds()) if interval is not None else None,
        "last_update_success": coordinator.last_update_success,
        "last_update_success_time": success_time.isoformat() if success_time is not None else None,
        "last_error_code": coordinator.last_error_code,
        # Seconds only: the token itself must never leave the process.
        "token_expires_in_seconds": round(expires_in) if expires_in is not None else None,
        "thermostats": async_redact_data(
            [_thermostat_diagnostics(thermostat) for thermostat in (coordinator.data or {}).values()],
            TO_REDACT,
        ),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    diagnostics: dict[str, Any] = {
        "integration_version": str(integration.version),
        "api_host": API_BASE_URL,
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
    }

    # HA serves diagnostics for entries that never finished setting up too - a migrated
    # ha-daikinone entry waiting for reauth has no runtime data at all.
    coordinator: DaikinOneCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return diagnostics
    return diagnostics | _coordinator_diagnostics(coordinator)
