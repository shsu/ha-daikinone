"""Typed models for the official Daikin One Open API.

Parsing is deliberately tolerant: unknown keys are ignored, missing or invalid values
become ``None`` (never zero), and unknown enum integers map to ``UNKNOWN`` so future
API values cannot break the integration. Ground truth: tests/spec/daikin_open_api.json.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
import math
from typing import Any, Self

# Value ranges used to reject sentinel / garbage numbers (temperatures are Celsius).
TEMP_RANGE = (-60.0, 80.0)
HUMIDITY_RANGE = (0.0, 100.0)
DELTA_RANGE = (0.0, 20.0)


class _TolerantIntEnum(IntEnum):
    """IntEnum that maps any unrecognised value to the UNKNOWN member."""

    @classmethod
    def _missing_(cls, value: object) -> Self:
        return cls["UNKNOWN"]


class Mode(_TolerantIntEnum):
    """Thermostat mode (documented enum)."""

    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO = 3
    EMERGENCY_HEAT = 4
    UNKNOWN = -1


class ModeLimit(_TolerantIntEnum):
    """Thermostat mode limits (documented enum)."""

    NONE = 0
    ALL = 1
    HEAT_ONLY = 2
    COOL_ONLY = 3
    UNKNOWN = -1


class EquipmentStatus(_TolerantIntEnum):
    """HVAC equipment status (documented enum)."""

    COOL = 1
    OVERCOOL_DEHUM = 2
    HEAT = 3
    FAN = 4
    IDLE = 5
    UNKNOWN = -1


class SystemFan(_TolerantIntEnum):
    """System fan state (read-only, documented enum)."""

    AUTO = 0
    ON = 1
    UNKNOWN = -1


class FanCirculate(_TolerantIntEnum):
    """Fan circulation mode (documented enum, unitary systems only)."""

    OFF = 0
    ALWAYS_ON = 1
    SCHEDULE = 2
    UNKNOWN = -1


class FanCirculateSpeed(_TolerantIntEnum):
    """Fan circulation speed (documented enum, unitary systems only)."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    UNKNOWN = -1


def _num(value: Any, low: float, high: float) -> float | None:
    """Parse a finite number within [low, high]; anything else becomes None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        return None
    return result


def _flag(value: Any) -> bool | None:
    """Parse a boolean documented as either bool or int 0/1 (never other ints)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _enum[E: _TolerantIntEnum](cls: type[E], value: Any) -> E | None:
    """Parse an enum int; unknown ints map to UNKNOWN, non-ints to None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        return None
    return cls(value)


def _string(value: Any) -> str | None:
    """Parse a non-empty string; anything else becomes None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class ThermostatState:
    """State payload of GET /v1/devices/{id}; every field optional."""

    equipment_status: EquipmentStatus | None = None
    mode: Mode | None = None
    mode_limit: ModeLimit | None = None
    em_heat_available: bool | None = None
    fan: SystemFan | None = None
    fan_circulate: FanCirculate | None = None
    fan_circulate_speed: FanCirculateSpeed | None = None
    heat_setpoint: float | None = None
    cool_setpoint: float | None = None
    setpoint_delta: float | None = None
    setpoint_minimum: float | None = None
    setpoint_maximum: float | None = None
    temp_indoor: float | None = None
    hum_indoor: float | None = None
    temp_outdoor: float | None = None
    hum_outdoor: float | None = None
    schedule_enabled: bool | None = None
    geofencing_enabled: bool | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ThermostatState:
        """Build a state from an API payload, tolerating missing/invalid/unknown values."""
        return cls(
            equipment_status=_enum(EquipmentStatus, data.get("equipmentStatus")),
            mode=_enum(Mode, data.get("mode")),
            mode_limit=_enum(ModeLimit, data.get("modeLimit")),
            em_heat_available=_flag(data.get("modeEmHeatAvailable")),
            fan=_enum(SystemFan, data.get("fan")),
            fan_circulate=_enum(FanCirculate, data.get("fanCirculate")),
            fan_circulate_speed=_enum(FanCirculateSpeed, data.get("fanCirculateSpeed")),
            heat_setpoint=_num(data.get("heatSetpoint"), *TEMP_RANGE),
            cool_setpoint=_num(data.get("coolSetpoint"), *TEMP_RANGE),
            setpoint_delta=_num(data.get("setpointDelta"), *DELTA_RANGE),
            setpoint_minimum=_num(data.get("setpointMinimum"), *TEMP_RANGE),
            setpoint_maximum=_num(data.get("setpointMaximum"), *TEMP_RANGE),
            temp_indoor=_num(data.get("tempIndoor"), *TEMP_RANGE),
            hum_indoor=_num(data.get("humIndoor"), *HUMIDITY_RANGE),
            temp_outdoor=_num(data.get("tempOutdoor"), *TEMP_RANGE),
            hum_outdoor=_num(data.get("humOutdoor"), *HUMIDITY_RANGE),
            schedule_enabled=_flag(data.get("scheduleEnabled")),
            geofencing_enabled=_flag(data.get("geofencingEnabled")),
        )


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    """One thermostat from GET /v1/devices (the only source of name/model/firmware)."""

    id: str
    name: str
    model: str | None = None
    firmware_version: str | None = None
    location_name: str | None = None

    @classmethod
    def from_json(cls, location: Mapping[str, Any], device: Mapping[str, Any]) -> DeviceSummary | None:
        """Build a summary; returns None (caller skips) when the id is missing."""
        device_id = _string(device.get("id"))
        if device_id is None:
            return None
        return cls(
            id=device_id,
            name=_string(device.get("name")) or device_id,
            model=_string(device.get("model")),
            firmware_version=_string(device.get("firmwareVersion")),
            location_name=_string(location.get("locationName")),
        )


@dataclass(frozen=True, slots=True)
class Thermostat:
    """A thermostat as tracked by the coordinator: identity + last state + reachability."""

    summary: DeviceSummary
    state: ThermostatState = field(default_factory=ThermostatState)
    online: bool = True
