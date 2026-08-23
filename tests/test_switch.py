"""Schedule switch: payload, optimistic state and the coalesced verification read."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.const import DOMAIN

from .conftest import DEVICE_IDS, DEVICES_URL, PUT_URL_RE, TOKEN_URL, calls

DEVICES_PATH = "/v1/devices"
SCHEDULE_PATH = f"{DEVICES_PATH}/dev1/schedule"


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


def _mock_account(mock: AiohttpClientMocker, **detail: Any) -> None:
    """Register a full happy-path account."""
    mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    mock.get(DEVICES_URL, json=load_json_array_fixture("devices.json"))
    payload = load_json_object_fixture("device_oneplus.json") | detail
    for device_id in DEVICE_IDS:
        mock.get(f"{DEVICES_URL}/{device_id}", json=payload)
    mock.put(PUT_URL_RE, json={"message": "Write sent"})


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Set the entry up and return dev1's schedule switch entity id."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(SWITCH_DOMAIN, DOMAIN, "dev1-schedule")
    assert entity_id is not None
    return entity_id


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    """Invoke a switch service and wait for it."""
    await hass.services.async_call(SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def test_schedule_state_comes_from_the_thermostat(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`scheduleEnabled` drives the switch state."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    assert hass.states.get(entity_id).state == STATE_ON


async def test_turning_the_schedule_off_writes_and_applies_optimistically(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """`PUT /schedule` carries the single documented field and the UI updates at once."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)

    await _call(hass, SERVICE_TURN_OFF, entity_id)

    put = calls(aioclient_mock, "PUT", SCHEDULE_PATH)
    assert len(put) == 1
    assert put[0][2] == {"scheduleEnabled": False}
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_turning_the_schedule_back_on(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The opposite write sends `true`."""
    _mock_account(aioclient_mock, scheduleEnabled=False)
    entity_id = await _setup(hass, mock_config_entry)
    assert hass.states.get(entity_id).state == STATE_OFF

    await _call(hass, SERVICE_TURN_ON, entity_id)

    assert calls(aioclient_mock, "PUT", SCHEDULE_PATH)[0][2] == {"scheduleEnabled": True}
    assert hass.states.get(entity_id).state == STATE_ON


async def test_verification_read_waits_the_documented_settle_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Daikin requires >= 15 s before reading back a write; nothing is read before that."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)
    before = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    await _call(hass, SERVICE_TURN_OFF, entity_id)

    await _advance(hass, freezer, 14)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before

    await _advance(hass, freezer, 2)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before + 1

    # And exactly one: the verification read is not a new polling cadence.
    await _advance(hass, freezer, 2)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before + 1


async def test_consecutive_writes_coalesce_into_a_single_verification(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Writes at 0, 5 and 10 s are verified once, 15 s after the last one."""
    _mock_account(aioclient_mock)
    entity_id = await _setup(hass, mock_config_entry)
    before = len(calls(aioclient_mock, "GET", DEVICES_PATH))

    await _call(hass, SERVICE_TURN_OFF, entity_id)
    freezer.tick(5)
    await _call(hass, SERVICE_TURN_ON, entity_id)
    freezer.tick(5)
    await _call(hass, SERVICE_TURN_OFF, entity_id)
    assert len(calls(aioclient_mock, "PUT", SCHEDULE_PATH)) == 3

    # 24.5 s: still inside the settle window of the write at t=10.
    await _advance(hass, freezer, 14)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before

    await _advance(hass, freezer, 2)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before + 1

    # Well past the settle window but short of the 180 s poll: no further reads.
    await _advance(hass, freezer, 34)
    assert len(calls(aioclient_mock, "GET", DEVICES_PATH)) == before + 1
