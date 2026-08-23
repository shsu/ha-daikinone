"""Constants for the Daikin One integration (official Daikin One Open API only)."""

from __future__ import annotations

from typing import Final

from homeassistant.const import CONF_API_KEY, CONF_EMAIL, CONF_SCAN_INTERVAL, Platform

DOMAIN: Final = "daikinone"
MANUFACTURER: Final = "Daikin"

# Config entry data keys. CONF_EMAIL / CONF_API_KEY / CONF_SCAN_INTERVAL are re-exported from HA so the
# stored keys are the conventional "email" / "api_key" / "scan_interval".
CONF_INTEGRATOR_TOKEN: Final = "integrator_token"
# Legacy ha-daikinone key (0 = human-readable sensor unique ids, 1 = key-based). Absent on new entries.
CONF_UID_SCHEMA: Final = "entity_uid_schema_version"

__all__ = [
    "CONF_API_KEY",
    "CONF_EMAIL",
    "CONF_INTEGRATOR_TOKEN",
    "CONF_SCAN_INTERVAL",
    "CONF_UID_SCHEMA",
    "DOMAIN",
    "JITTER_SECONDS",
    "MANUFACTURER",
    "MAX_BACKOFF_SECONDS",
    "MAX_SCAN_INTERVAL",
    "MIN_SCAN_INTERVAL",
    "PLATFORMS",
    "PRESET_EMERGENCY_HEAT",
    "VERIFY_DELAY_SECONDS",
]

# Daikin "API USAGE LIMITS": poll no faster than every 3 minutes, wait >= 15 s after a write before reading.
MIN_SCAN_INTERVAL: Final = 180
MAX_SCAN_INTERVAL: Final = 3600
JITTER_SECONDS: Final = 10
VERIFY_DELAY_SECONDS: Final = 15
MAX_BACKOFF_SECONDS: Final = 1800

PRESET_EMERGENCY_HEAT: Final = "emergency_heat"

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
