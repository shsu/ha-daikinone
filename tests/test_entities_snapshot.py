"""Entity snapshots plus the unique-id / device-registry contract migrated installs rely on."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

from homeassistant.const import CONF_API_KEY, CONF_EMAIL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.daikinone.const import CONF_INTEGRATOR_TOKEN, CONF_UID_SCHEMA, DOMAIN

from .conftest import API_KEY, EMAIL, INTEGRATOR_TOKEN

ALL_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
#: The unique ids ha-daikinone issued; changing any of them orphans a user's entity.
EXPECTED_UNIQUE_IDS = {
    (Platform.CLIMATE, "dev1-climate"),
    (Platform.SENSOR, "dev1-indoor_temperature"),
    (Platform.SENSOR, "dev1-indoor_humidity"),
    (Platform.SENSOR, "dev1-outdoor_temperature"),
    (Platform.SENSOR, "dev1-outdoor_humidity"),
    (Platform.SENSOR, "dev1-system_fan"),
    (Platform.BINARY_SENSOR, "dev1-online"),
    (Platform.SWITCH, "dev1-schedule"),
    (Platform.SELECT, "dev1-fan_circulate"),
    (Platform.SELECT, "dev1-fan_speed"),
}
LEGACY_SENSOR_IDS = {
    "dev1-Indoor Temperature",
    "dev1-Indoor Humidity",
    "dev1-Outdoor Temperature",
    "dev1-Outdoor Humidity",
}


@pytest.fixture(autouse=True)
def _deterministic_jitter() -> Iterator[None]:
    """Remove the coordinator's poll jitter so snapshots do not depend on it."""
    with patch("custom_components.daikinone.coordinator.random.uniform", return_value=0.0):
        yield


@pytest.fixture
def legacy_schema_entry() -> MockConfigEntry:
    """A migrated ha-daikinone entry that still uses the schema-0 sensor unique ids."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        unique_id=EMAIL.lower(),
        title=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_API_KEY: API_KEY,
            CONF_INTEGRATOR_TOKEN: INTEGRATOR_TOKEN,
            CONF_UID_SCHEMA: 0,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, platforms: list[Platform]) -> None:
    """Set the entry up with only the given platforms, and every entity enabled."""
    entry.add_to_hass(hass)
    with (
        patch("custom_components.daikinone.PLATFORMS", platforms),
        patch("homeassistant.helpers.entity.Entity.entity_registry_enabled_default", return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_api")
@pytest.mark.parametrize("platform", ALL_PLATFORMS)
async def test_platform_snapshot(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    platform: Platform,
) -> None:
    """Every entity of every platform matches its recorded registry entry and state."""
    await _setup(hass, mock_config_entry, [platform])

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("mock_api")
async def test_unique_ids_match_the_compatibility_contract(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """The unique ids inherited from ha-daikinone are reproduced exactly."""
    await _setup(hass, mock_config_entry, ALL_PLATFORMS)

    registered = {
        (entry.domain, entry.unique_id)
        for entry in er.async_entries_for_config_entry(entity_registry, mock_config_entry.entry_id)
    }
    assert registered >= EXPECTED_UNIQUE_IDS


@pytest.mark.usefixtures("mock_api")
async def test_schema_zero_entries_keep_the_human_readable_sensor_ids(
    hass: HomeAssistant, legacy_schema_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Schema 0 sensors keep their historic ids; the other platforms never had two schemas."""
    await _setup(hass, legacy_schema_entry, [Platform.SENSOR, Platform.CLIMATE, Platform.SELECT])

    registered = {
        entry.unique_id for entry in er.async_entries_for_config_entry(entity_registry, legacy_schema_entry.entry_id)
    }
    assert registered >= LEGACY_SENSOR_IDS
    assert "dev1-indoor_temperature" not in registered
    # The diagnostic sensor is new in this integration, so it uses the key-based id.
    assert "dev1-system_fan" in registered
    assert registered >= {"dev1-climate", "dev1-fan_circulate", "dev1-fan_speed"}


@pytest.mark.usefixtures("mock_api")
async def test_noisy_equipment_sensors_are_disabled_by_default(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """The four equipment-status binary sensors are opt-in; online/geofencing are not."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.daikinone.PLATFORMS", [Platform.BINARY_SENSOR]):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    disabled = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(entity_registry, mock_config_entry.entry_id)
        if entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    }
    assert disabled == {"dev1-heating", "dev1-cooling", "dev1-dehumidifying", "dev1-fan_running"} | {
        f"dev{index}-{key}" for index in (2, 3) for key in ("heating", "cooling", "dehumidifying", "fan_running")
    }


@pytest.mark.usefixtures("mock_api")
async def test_device_registry_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """One device per thermostat, named by location because the account has two."""
    await _setup(hass, mock_config_entry, [Platform.CLIMATE])

    device = device_registry.async_get_device(identifiers={(DOMAIN, "dev1")})
    assert device is not None
    assert device.manufacturer == "Daikin"
    assert device.model == "ONEPLUS"
    assert device.sw_version == "3.1.7"
    assert device.name == "Home Main Floor"

    cabin = device_registry.async_get_device(identifiers={(DOMAIN, "dev3")})
    assert cabin is not None
    assert (cabin.model, cabin.sw_version, cabin.name) == ("TOUCH", "2.3.5", "Cabin Main Room")
