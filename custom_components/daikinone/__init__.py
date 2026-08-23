"""The Daikin One integration (official Daikin One Open API only).

Entries created by `zlangbert/ha-daikinone` (version 1, email + password) are migrated to
version 2 in place: the password is kept until reauthentication with an API key and an
Integrator Token succeeds, so nothing is lost if the user cancels the reauth flow.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DaikinOneClient
from .const import CONF_INTEGRATOR_TOKEN, CONF_UID_SCHEMA, DOMAIN, PLATFORMS
from .coordinator import DaikinOneCoordinator

type DaikinOneConfigEntry = ConfigEntry[DaikinOneCoordinator]

__all__ = [
    "DaikinOneConfigEntry",
    "async_migrate_entry",
    "async_remove_config_entry_device",
    "async_setup_entry",
    "async_unload_entry",
]


async def async_setup_entry(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> bool:
    """Set up Daikin One from a config entry."""
    if not entry.data.get(CONF_API_KEY) or not entry.data.get(CONF_INTEGRATOR_TOKEN):
        # A migrated ha-daikinone entry: only a password is stored, which the official API
        # cannot use. Reauthentication collects the API key and the Integrator Token.
        raise ConfigEntryAuthFailed("An API key and an Integrator Token are required")

    client = DaikinOneClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_API_KEY],
        entry.data[CONF_INTEGRATOR_TOKEN],
    )
    coordinator = DaikinOneCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DaikinOneConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a ha-daikinone version 1 entry to this integration's version 2 schema."""
    if entry.version > 2:
        # Downgrades are not supported: a newer HA wrote a schema this code cannot read.
        return False

    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            unique_id=entry.unique_id or str(entry.data[CONF_EMAIL]).lower(),
            # The legacy password stays until reauthentication succeeds; the unique-id
            # schema version decides which historic sensor unique ids to reuse.
            data={**entry.data, CONF_UID_SCHEMA: entry.data.get(CONF_UID_SCHEMA, 0)},
            version=2,
            minor_version=1,
        )

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: DaikinOneConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting devices that no longer exist in the Daikin account."""
    if config_entry.state is not ConfigEntryState.LOADED:
        # `runtime_data` only exists while the entry is set up, and HA offers manual device
        # deletion regardless of that. An entry parked in reauth (the ha-daikinone
        # migration path) is exactly when a user prunes orphaned legacy devices, so there
        # is no live device list to veto the deletion with.
        return True
    thermostats = config_entry.runtime_data.data or {}
    return not any(domain == DOMAIN and key in thermostats for domain, key in device_entry.identifiers)
