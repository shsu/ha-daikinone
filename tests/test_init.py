"""Setup, unload, reload, options clamping and ha-daikinone migration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    STATE_UNAVAILABLE,
    EntityStateAttribute,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, load_json_object_fixture
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone import async_migrate_entry, async_remove_config_entry_device
from custom_components.daikinone.const import CONF_INTEGRATOR_TOKEN, CONF_UID_SCHEMA, DOMAIN, PLATFORMS

from .conftest import (
    ACCESS_TOKEN,
    API_KEY,
    DEVICE_IDS,
    DEVICES_URL,
    EMAIL,
    INTEGRATOR_TOKEN,
    TOKEN_URL,
    sequence,
)

NEW_CREDENTIALS = {
    CONF_EMAIL: EMAIL,
    CONF_API_KEY: API_KEY,
    CONF_INTEGRATOR_TOKEN: INTEGRATOR_TOKEN,
}
#: ha-daikinone's schema-0 sensor unique-id suffixes and their schema-1 replacements.
LEGACY_SENSOR_NAMES = {
    "Indoor Temperature": "indoor_temperature",
    "Indoor Humidity": "indoor_humidity",
    "Outdoor Temperature": "outdoor_temperature",
    "Outdoor Humidity": "outdoor_humidity",
}


def _is_live(hass: HomeAssistant, entity_id: str) -> bool:
    """True when a real entity is behind ``entity_id``.

    A registry row nothing registers for has no state at all; one whose entity was removed
    keeps an unavailable placeholder flagged ``restored``. Neither counts as live.
    """
    state = hass.states.get(entity_id)
    return state is not None and not state.attributes.get(EntityStateAttribute.RESTORED)


TOKEN_OK: dict[str, Any] = {"json": load_json_object_fixture("token.json")}

#: Everything the integration must never write to a log or an entity attribute.
CREDENTIALS = (EMAIL, API_KEY, INTEGRATOR_TOKEN, ACCESS_TOKEN)


@pytest.fixture(autouse=True)
def _only_implemented_platforms() -> Iterator[None]:
    """Forward only the platform modules that exist on disk.

    Keeps these tests independent of whether the entity platforms have landed yet; once
    they all exist this is a no-op that forwards the full PLATFORMS list.
    """
    integration = Path(__file__).parent.parent / "custom_components" / "daikinone"
    available = [platform for platform in PLATFORMS if (integration / f"{platform.value}.py").is_file()]
    with patch("custom_components.daikinone.PLATFORMS", available):
        yield


async def test_setup_loads_every_thermostat(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """A healthy account loads and the coordinator holds all three thermostats."""
    assert init_integration.state is ConfigEntryState.LOADED
    assert set(init_integration.runtime_data.data) == set(DEVICE_IDS)


async def test_unload_after_a_write_leaves_no_timer(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Unloading immediately after a write cancels the pending verification read.

    The autouse ``verify_cleanup`` fixture fails the test on a lingering HassJob timer.
    """
    await init_integration.runtime_data.async_set_schedule_enabled("dev1", False)

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED
    # HA core keeps registry-backed entities in the state machine after an unload, as
    # unavailable + restored placeholders; nothing may still report a live value.
    climate_states = [state for state in hass.states.async_all() if state.entity_id.startswith("climate.")]
    assert all(
        state.state == STATE_UNAVAILABLE and state.attributes.get(EntityStateAttribute.RESTORED)
        for state in climate_states
    )


async def test_unload_during_an_in_flight_write_leaves_no_timer(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A write still awaiting its PUT when the entry unloads must not arm a timer afterwards.

    Any entity service call can overlap a reload (options change, reauth, the Reload
    button); the write completes after ``async_shutdown`` and would otherwise schedule a
    15 s verification read that nothing owns. ``verify_cleanup`` fails on that timer.
    """
    coordinator = init_integration.runtime_data
    started, gate = asyncio.Event(), asyncio.Event()

    async def _blocked_put(*args: Any, **kwargs: Any) -> None:
        started.set()
        await gate.wait()

    with patch.object(coordinator.client, "async_set_schedule_enabled", side_effect=_blocked_put):
        write = asyncio.create_task(coordinator.async_set_schedule_enabled("dev1", False))
        await started.wait()

        assert await hass.config_entries.async_unload(init_integration.entry_id)
        await hass.async_block_till_done()

        gate.set()
        await write
    await hass.async_block_till_done()

    assert coordinator._verify_cancel is None


async def test_reload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The entry can be reloaded and comes back with fresh data."""
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        assert await hass.config_entries.async_reload(init_integration.entry_id)
        await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.LOADED
    assert set(init_integration.runtime_data.data) == set(DEVICE_IDS)


async def test_scan_interval_is_clamped_to_daikins_floor(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A stored interval below Daikin's 3-minute limit is raised to 180 s."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_SCAN_INTERVAL: 60})

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.runtime_data.base_interval == 180


async def test_missing_credentials_start_reauth(hass: HomeAssistant, mock_api: AiohttpClientMocker) -> None:
    """A v2 entry without an API key cannot be set up and asks for reauthentication."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        unique_id=EMAIL.lower(),
        data={CONF_EMAIL: EMAIL, CONF_INTEGRATOR_TOKEN: INTEGRATOR_TOKEN},
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


@pytest.mark.parametrize(
    ("legacy_data", "expected_schema"),
    [
        ({CONF_UID_SCHEMA: 0}, 0),
        ({CONF_UID_SCHEMA: 1}, 1),
        ({}, 0),
    ],
    ids=["schema-0", "schema-1", "schema-missing"],
)
async def test_migrate_v1_entry(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    legacy_data: dict[str, Any],
    expected_schema: int,
) -> None:
    """A ha-daikinone v1 entry migrates to v2, keeps its password and asks for reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        title="Daikin One",
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "legacy-password-placeholder", **legacy_data},
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.minor_version == 1
    assert entry.unique_id == EMAIL.lower()
    assert entry.data[CONF_UID_SCHEMA] == expected_schema
    # The password survives migration; it is only dropped once reauth succeeds.
    assert CONF_PASSWORD in entry.data
    assert entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH

    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], NEW_CREDENTIALS)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_API_KEY] == API_KEY
    assert entry.data[CONF_UID_SCHEMA] == expected_schema
    assert entry.state is ConfigEntryState.LOADED


async def test_migration_refuses_future_versions(hass: HomeAssistant, mock_api: AiohttpClientMocker) -> None:
    """An entry written by a newer version is not downgraded."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, minor_version=1, data={CONF_EMAIL: EMAIL})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR


@pytest.mark.parametrize("schema", [0, 1])
async def test_legacy_entity_ids_survive_migration(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    entity_registry: er.EntityRegistry,
    schema: int,
) -> None:
    """Every registry row ha-daikinone created is reclaimed by a live entity after migration.

    The user-visible contract is that the entity ids keep working, so the seeded rows must
    end up *backed by a live state*, not merely present in the registry: a row whose unique
    id moved stays in the registry forever (nothing deletes it) but never gets a state, and
    the entity reappears under a freshly generated id. The registry-wide sweep at the end
    catches that same failure from the other side (an orphan row plus its replacement).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        title="Daikin One",
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "legacy-password-placeholder", CONF_UID_SCHEMA: schema},
    )
    entry.add_to_hass(hass)

    # Object ids as a ha-daikinone user would have them: mostly renamed by hand, which is
    # exactly why the unique ids — not the generated ids — have to be preserved.
    legacy_unique_ids = {
        ("climate", "dev1-climate"): "daikin_one_main_floor",
        ("select", "dev1-fan_speed"): "main_floor_fan_speed",
    } | {
        ("sensor", f"dev1-{legacy if schema == 0 else key}"): f"main_floor_{key}"
        for legacy, key in LEGACY_SENSOR_NAMES.items()
    }
    before = {
        unique_id: entity_registry.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry, suggested_object_id=object_id
        ).entity_id
        for (domain, unique_id), object_id in legacy_unique_ids.items()
    }

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        await hass.config_entries.flow.async_configure(flow["flow_id"], NEW_CREDENTIALS)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    for domain, unique_id in legacy_unique_ids:
        assert entity_registry.async_get_entity_id(domain, DOMAIN, unique_id) == before[unique_id]
    assert [entity_id for entity_id in before.values() if not _is_live(hass, entity_id)] == []
    # No orphans: every enabled row of this entry is backed by a live entity, so a moved
    # unique id cannot hide behind a leftover row plus a new one.
    assert [
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if registry_entry.disabled_by is None and not _is_live(hass, registry_entry.entity_id)
    ] == []
    assert not [
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if registry_entry.entity_id.endswith("_2")
    ]


async def test_remove_config_entry_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Only devices that vanished from the account may be deleted by hand."""
    known = device_registry.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, "dev1")}
    )
    ghost = device_registry.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, "gone")}
    )

    assert await async_remove_config_entry_device(hass, init_integration, known) is False
    assert await async_remove_config_entry_device(hass, init_integration, ghost) is True


async def test_remove_device_from_an_entry_that_never_loaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A migrated entry waiting for reauth still lets the user delete orphaned devices."""
    mock_config_entry.add_to_hass(hass)
    ghost = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "DM96VC0803BNAB-PLACEHOLDER-SERIAL")},
    )
    thermostat = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id, identifiers={(DOMAIN, "dev1")}
    )

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert await async_remove_config_entry_device(hass, mock_config_entry, ghost) is True
    # Without a running coordinator there is no live device list to veto a deletion; HA
    # recreates a still-existing thermostat on the next successful poll anyway.
    assert await async_remove_config_entry_device(hass, mock_config_entry, thermostat) is True


async def test_auth_failure_while_polling_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Two 401s in a row on the device list push the entry into reauthentication."""
    mock_api.clear_requests()
    mock_api.post(TOKEN_URL, json={"accessToken": ACCESS_TOKEN, "accessTokenExpiresIn": 900})
    mock_api.get(DEVICES_URL, side_effect=sequence({"status": 401, "json": {"messages": "NotAuthorizedException"}}))

    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert not init_integration.runtime_data.last_update_success
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_migrate_entry_called_directly(hass: HomeAssistant) -> None:
    """The version guard is exercised without HA's own pre-check."""
    future = MockConfigEntry(domain=DOMAIN, version=3, minor_version=1, data={CONF_EMAIL: EMAIL})
    future.add_to_hass(hass)
    assert await async_migrate_entry(hass, future) is False

    current = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1, data={CONF_EMAIL: EMAIL})
    current.add_to_hass(hass)
    assert await async_migrate_entry(hass, current) is True
    assert current.data == {CONF_EMAIL: EMAIL}


@pytest.mark.parametrize(
    ("token", "devices", "expected_state"),
    [
        (
            {"status": 403, "json": {"messages": "NotAuthorizedException"}},
            {"status": 403},
            ConfigEntryState.SETUP_ERROR,
        ),
        (TOKEN_OK, {"status": 500}, ConfigEntryState.SETUP_RETRY),
    ],
    ids=["auth_failed", "not_ready"],
)
async def test_setup_failure_never_logs_a_credential(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    *,
    token: dict[str, Any],
    devices: dict[str, Any],
    expected_state: ConfigEntryState,
) -> None:
    """Home Assistant logs ``entry.title`` verbatim on both setup-failure paths.

    A transient Daikin outage during setup is routine, and its INFO line lands in every
    log file a user attaches to an issue, so nothing the config flow puts in the title may
    be a credential. The entry is created by the real flow so the production title is the
    one under test.
    """
    caplog.set_level(logging.DEBUG)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], NEW_CREDENTIALS)
        await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED

    mock_api.clear_requests()
    mock_api.post(TOKEN_URL, **token)
    mock_api.get(DEVICES_URL, **devices)
    caplog.clear()

    assert not await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    entry.async_cancel_retry_setup()

    assert entry.state is expected_state
    assert "for daikinone integration" in caplog.text
    for credential in CREDENTIALS:
        assert credential not in caplog.text


async def test_no_credential_reaches_the_state_machine(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """No entity may expose a credential through its state or its attributes."""
    states = hass.states.async_all()
    assert states

    dumped = json.dumps([{state.entity_id: dict(state.attributes)} for state in states], default=str)
    assert "friendly_name" in dumped  # the dump really holds the attributes
    for credential in CREDENTIALS:
        assert credential not in dumped
