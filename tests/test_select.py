"""Fan circulation selects: registration, payloads and the unsupported-equipment repair."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.select import ATTR_OPTION, ATTR_OPTIONS, DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import DOMAIN

from .conftest import DEVICE_IDS, DEVICES_URL, PUT_URL_RE, TOKEN_URL, calls, sequence

DEVICES_PATH = "/v1/devices"
FAN_PATH = f"{DEVICES_PATH}/dev1/fan"
FAN_ISSUE_ID = "fan_unsupported_dev1"
WRITE_OK = {"message": "Write sent"}
# Daikin's documented rejection for a system that has no fan circulation control.
FAN_REJECTED = {"status": 400, "json": {"messages": "Invalid request body"}}


@pytest.fixture(autouse=True)
def _deterministic_scheduling() -> Iterator[None]:
    """Pin both sources of scheduling randomness so tick() maths is exact."""
    with (
        patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0),
        patch("homeassistant.helpers.update_coordinator.randint", return_value=500000),
    ):
        yield


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int) -> None:
    """Advance the clock and run any timer that comes due (they are background tasks)."""
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _mock_account(mock: AiohttpClientMocker, *, fan: dict[str, Any] | None = None) -> None:
    """Register a full account; `fan` overrides dev1's PUT /fan response."""
    if fan is not None:
        mock.put(f"{DEVICES_URL}/dev1/fan", **fan)
    mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))
    for device_id in DEVICE_IDS:
        mock.get(f"{DEVICES_URL}/{device_id}", json=load_json_object_fixture("device_oneplus.json"))
    mock.put(PUT_URL_RE, json=WRITE_OK)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, str]:
    """Set the entry up and return dev1's (fan_circulate, fan_speed) entity ids."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    circulate = registry.async_get_entity_id(SELECT_DOMAIN, DOMAIN, "dev1-fan_circulate")
    speed = registry.async_get_entity_id(SELECT_DOMAIN, DOMAIN, "dev1-fan_speed")
    assert circulate is not None
    assert speed is not None
    return circulate, speed


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    """Invoke select.select_option and wait for it."""
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


async def test_both_selects_are_registered_enabled_and_configuration_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
) -> None:
    """Owner's decision: the fan selects ship enabled, as configuration entities."""
    _mock_account(aioclient_mock)
    circulate, speed = await _setup(hass, mock_config_entry)

    for entity_id in (circulate, speed):
        entry = entity_registry.async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is None
        assert entry.entity_category is EntityCategory.CONFIG


async def test_current_options_come_from_the_thermostat(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`fanCirculate` 0 and `fanCirculateSpeed` 1 read back as off / medium."""
    _mock_account(aioclient_mock)
    circulate, speed = await _setup(hass, mock_config_entry)

    circulate_state = hass.states.get(circulate)
    assert circulate_state.state == "off"
    assert circulate_state.attributes[ATTR_OPTIONS] == ["off", "always_on", "schedule"]

    speed_state = hass.states.get(speed)
    assert speed_state.state == "medium"
    assert speed_state.attributes[ATTR_OPTIONS] == ["low", "medium", "high"]


async def test_unknown_enum_values_have_no_option(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An undocumented integer never invents an option."""
    payload = load_json_object_fixture("device_oneplus.json") | {"fanCirculate": 9}
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))
    for device_id in DEVICE_IDS:
        aioclient_mock.get(f"{DEVICES_URL}/{device_id}", json=payload)
    aioclient_mock.put(PUT_URL_RE, json=WRITE_OK)
    circulate, _ = await _setup(hass, mock_config_entry)

    assert hass.states.get(circulate).state == "unknown"


async def test_selecting_a_circulation_mode_carries_the_speed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`PUT /fan` always carries both documented fields."""
    _mock_account(aioclient_mock)
    circulate, _ = await _setup(hass, mock_config_entry)

    await _select(hass, circulate, "schedule")

    put = calls(aioclient_mock, "PUT", FAN_PATH)
    assert len(put) == 1
    assert put[0][2] == {"fanCirculate": 2, "fanCirculateSpeed": 1}
    assert hass.states.get(circulate).state == "schedule"


async def test_selecting_a_speed_carries_the_circulation_mode(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The other side of the pair comes from the local snapshot."""
    _mock_account(aioclient_mock)
    _, speed = await _setup(hass, mock_config_entry)

    await _select(hass, speed, "high")

    assert calls(aioclient_mock, "PUT", FAN_PATH)[0][2] == {"fanCirculate": 0, "fanCirculateSpeed": 2}
    assert hass.states.get(speed).state == "high"


async def test_no_fan_write_happens_without_a_service_call(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """VRV / split safety: setting the entities up must never probe the equipment."""
    _mock_account(aioclient_mock)
    await _setup(hass, mock_config_entry)

    assert not calls(aioclient_mock, "PUT", FAN_PATH)
    assert ir.async_get(hass).async_get_issue(DOMAIN, FAN_ISSUE_ID) is None


async def test_rejected_fan_write_raises_and_files_a_repair(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Rejected once -> error + repair issue; accepted later -> the issue disappears."""
    _mock_account(aioclient_mock, fan={"side_effect": sequence(FAN_REJECTED, {"json": WRITE_OK})})
    circulate, _ = await _setup(hass, mock_config_entry)
    before = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    with pytest.raises(HomeAssistantError) as err:
        await _select(hass, circulate, "always_on")

    assert err.value.translation_key == "unsupported_capability"
    assert hass.states.get(circulate).state == "off"

    issue = issue_registry.async_get_issue(DOMAIN, FAN_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "fan_controls_unsupported"
    assert issue.severity is ir.IssueSeverity.WARNING

    # A failed write changes nothing, so there is nothing to verify either.
    await _advance(hass, freezer, 16)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before

    await _select(hass, circulate, "always_on")

    assert hass.states.get(circulate).state == "always_on"
    assert issue_registry.async_get_issue(DOMAIN, FAN_ISSUE_ID) is None
