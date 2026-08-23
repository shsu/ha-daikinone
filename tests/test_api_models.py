"""Unit tests for the tolerant parsing layer (`custom_components.daikinone.api.models`).

No Home Assistant instance and no HTTP: these are pure parsing tests. The contract is
"never raise, never invent a value": unknown keys are ignored, invalid or missing values
become ``None`` (never ``0``), and unrecognised enum integers become ``UNKNOWN`` so a
future Daikin firmware value cannot break the integration.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.daikinone.api.models import (
    DeviceSummary,
    EquipmentStatus,
    FanCirculate,
    FanCirculateSpeed,
    Mode,
    ModeLimit,
    SystemFan,
    Thermostat,
    ThermostatState,
)

SPEC: dict[str, Any] = json.loads((Path(__file__).parent / "spec" / "daikin_open_api.json").read_text(encoding="utf-8"))
EXAMPLE_2023: dict[str, Any] = SPEC["examples"]["device_response_2023"]
EXAMPLE_STUB: dict[str, Any] = SPEC["examples"]["device_response_schema_stub"]

TEMP_FIELDS = (
    ("tempIndoor", "temp_indoor"),
    ("tempOutdoor", "temp_outdoor"),
    ("heatSetpoint", "heat_setpoint"),
    ("coolSetpoint", "cool_setpoint"),
    ("setpointMinimum", "setpoint_minimum"),
    ("setpointMaximum", "setpoint_maximum"),
)
HUMIDITY_FIELDS = (("humIndoor", "hum_indoor"), ("humOutdoor", "hum_outdoor"))

# Values the API must never turn into a number: non-finite, sentinel, boolean or textual.
BAD_NUMBERS = (float("nan"), float("inf"), -float("inf"), 9999, -999, True, False, "warm", None)


def test_unknown_top_level_keys_are_ignored() -> None:
    """A payload with extra keys parses exactly like the documented one."""
    junk = {
        **EXAMPLE_2023,
        "somethingNew": 42,
        "nested": {"a": [1, 2, 3]},
        "modeEmHeatAvailableV2": "yes",
        "": None,
    }

    assert ThermostatState.from_json(junk) == ThermostatState.from_json(EXAMPLE_2023)


def test_all_fields_missing_gives_all_none() -> None:
    """An empty payload yields a state whose every field is None."""
    state = ThermostatState.from_json({})

    values = dataclasses.asdict(state)
    assert values, "ThermostatState must expose fields"
    assert all(value is None for value in values.values()), values


@pytest.mark.parametrize(
    ("key", "attribute", "raw", "enum_cls"),
    [
        ("mode", "mode", 7, Mode),
        ("equipmentStatus", "equipment_status", 0, EquipmentStatus),
        ("equipmentStatus", "equipment_status", 9, EquipmentStatus),
        ("modeLimit", "mode_limit", 12, ModeLimit),
        ("fanCirculateSpeed", "fan_circulate_speed", 5, FanCirculateSpeed),
        ("fan", "fan", 3, SystemFan),
    ],
)
def test_unknown_enum_ints_become_unknown(key: str, attribute: str, raw: int, enum_cls: type) -> None:
    """Undocumented enum integers (incl. the hidden 6-9 statuses) map to UNKNOWN, not an error."""
    state = ThermostatState.from_json({key: raw})

    value = getattr(state, attribute)
    assert value is enum_cls.UNKNOWN
    assert int(value) == -1


@pytest.mark.parametrize(("key", "attribute"), [*TEMP_FIELDS, *HUMIDITY_FIELDS])
@pytest.mark.parametrize("raw", BAD_NUMBERS, ids=repr)
def test_invalid_numerics_become_none(key: str, attribute: str, raw: Any) -> None:
    """NaN/inf/sentinels/booleans/strings never become a temperature or humidity."""
    state = ThermostatState.from_json({key: raw})

    assert getattr(state, attribute) is None


@pytest.mark.parametrize(("key", "attribute"), HUMIDITY_FIELDS)
@pytest.mark.parametrize("raw", [101, -1, 100.5])
def test_out_of_range_humidity_becomes_none(key: str, attribute: str, raw: float) -> None:
    """Humidity outside 0-100 % is rejected rather than clamped."""
    assert getattr(ThermostatState.from_json({key: raw}), attribute) is None


def test_non_finite_values_parsed_from_raw_json_text() -> None:
    """json.loads accepts NaN/Infinity literals, so tolerance must hold end to end."""
    payload = json.loads('{"tempIndoor": NaN, "tempOutdoor": Infinity, "humIndoor": -Infinity, "humOutdoor": 50}')

    state = ThermostatState.from_json(payload)

    assert state.temp_indoor is None
    assert state.temp_outdoor is None
    assert state.hum_indoor is None
    assert state.hum_outdoor == 50.0


@pytest.mark.parametrize(("raw", "expected"), [(0, False), (1, True), (False, False), (True, True)])
def test_em_heat_available_accepts_documented_int_and_bool(raw: Any, expected: bool) -> None:
    """The docs type this as Integer 0/1 but the example sends a bool; both are accepted."""
    assert ThermostatState.from_json({"modeEmHeatAvailable": raw}).em_heat_available is expected


@pytest.mark.parametrize("raw", [2, -1, "yes", "true", 1.5, None])
def test_em_heat_available_rejects_undocumented_values(raw: Any) -> None:
    """Anything outside bool / 0 / 1 is unknown, not silently truthy."""
    assert ThermostatState.from_json({"modeEmHeatAvailable": raw}).em_heat_available is None


@pytest.mark.parametrize(
    ("key", "attribute", "raw"), [("tempIndoor", "temp_indoor", -60), ("tempIndoor", "temp_indoor", 80)]
)
def test_temperature_boundaries_are_kept(key: str, attribute: str, raw: float) -> None:
    """The plausibility window is inclusive: -60 C and 80 C are real readings."""
    assert getattr(ThermostatState.from_json({key: raw}), attribute) == float(raw)


@pytest.mark.parametrize("raw", [0, 100])
def test_humidity_boundaries_are_kept(raw: int) -> None:
    """0 % and 100 % are valid humidity readings."""
    assert ThermostatState.from_json({"humIndoor": raw}).hum_indoor == float(raw)


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n", 123, {"id": "x"}])
def test_device_summary_requires_an_id(raw: Any) -> None:
    """A device entry without a usable id is skipped (returns None), never half-built."""
    assert DeviceSummary.from_json({"locationName": "Home"}, {"id": raw, "name": "T"}) is None


def test_device_summary_missing_id_key() -> None:
    """An absent id key behaves like an empty one."""
    assert DeviceSummary.from_json({}, {"name": "T"}) is None


@pytest.mark.parametrize("name", [None, "", "   "])
def test_device_summary_name_falls_back_to_id(name: Any) -> None:
    """A nameless thermostat is still addressable: the name defaults to the id."""
    summary = DeviceSummary.from_json({"locationName": "Home"}, {"id": " dev1 ", "name": name})

    assert summary is not None
    assert summary.id == "dev1"
    assert summary.name == "dev1"


def test_device_summary_optional_fields() -> None:
    """locationName/model/firmwareVersion are optional and stay None when absent."""
    summary = DeviceSummary.from_json({}, {"id": "dev1", "name": "Main Floor"})

    assert summary is not None
    assert summary.name == "Main Floor"
    assert summary.location_name is None
    assert summary.model is None
    assert summary.firmware_version is None


def test_device_summary_full_payload() -> None:
    """A complete documented device entry maps field for field."""
    summary = DeviceSummary.from_json(
        {"locationName": "Country House"},
        {"id": "dev1", "name": "Main Room", "model": "ONEPLUS", "firmwareVersion": "2.3.5", "extra": 1},
    )

    assert summary == DeviceSummary(
        id="dev1",
        name="Main Room",
        model="ONEPLUS",
        firmware_version="2.3.5",
        location_name="Country House",
    )


@pytest.mark.parametrize("example", ["device_response_2023", "device_response_schema_stub"], ids=str)
def test_documented_examples_parse(example: str) -> None:
    """Both payloads printed in the official docs parse without error or loss."""
    state = ThermostatState.from_json(SPEC["examples"][example])

    assert dataclasses.asdict(state), "state must not be empty"
    assert all(getattr(state, f.name) is not None for f in dataclasses.fields(state))


def test_documented_em_heat_int_bool_quirk_parses_identically() -> None:
    """The docs show `modeEmHeatAvailable` as `false` (2023 example) and `0` (schema stub)."""
    assert EXAMPLE_2023["modeEmHeatAvailable"] is False
    assert EXAMPLE_STUB["modeEmHeatAvailable"] == 0

    assert ThermostatState.from_json(EXAMPLE_2023).em_heat_available is False
    assert ThermostatState.from_json(EXAMPLE_STUB).em_heat_available is False


def test_example_2023_values() -> None:
    """Spot-check the documented 2023 example end to end."""
    state = ThermostatState.from_json(EXAMPLE_2023)

    assert state.mode is Mode.OFF
    assert state.equipment_status is EquipmentStatus.FAN
    assert state.mode_limit is ModeLimit.ALL
    assert state.fan is SystemFan.AUTO
    assert state.fan_circulate is FanCirculate.ALWAYS_ON
    assert state.fan_circulate_speed is FanCirculateSpeed.HIGH
    assert state.temp_indoor == 23.1
    assert state.hum_indoor == 42.0
    assert state.heat_setpoint == 17.1
    assert state.cool_setpoint == 20.0
    assert state.schedule_enabled is False
    assert state.geofencing_enabled is False


def test_thermostat_defaults_to_empty_online_state() -> None:
    """A Thermostat can be built from a summary alone (used when a device is unreachable)."""
    summary = DeviceSummary(id="dev1", name="Main Floor")

    thermostat = Thermostat(summary=summary)

    assert thermostat.online is True
    assert thermostat.state == ThermostatState()
