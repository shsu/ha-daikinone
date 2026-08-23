"""Async client for the official Daikin One Open API (integrator-api.daikinskyport.com)."""

from __future__ import annotations

from .auth import IntegratorAuth
from .client import DaikinOneClient
from .const import (
    API_BASE_URL,
    DEVICES_PATH,
    MAX_CONCURRENT_REQUESTS,
    REQUEST_TIMEOUT,
    TOKEN_PATH,
    TOKEN_REFRESH_MARGIN,
)
from .exceptions import (
    AuthError,
    DaikinAuthError,
    DaikinOneError,
    DeviceOfflineError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    InvalidRequestError,
    MalformedResponseError,
    RateLimitedError,
    ServerError,
    TokenExpiredError,
    TransportError,
    UnsupportedCapabilityError,
    error_for,
)
from .models import (
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

__all__ = [
    "API_BASE_URL",
    "DEVICES_PATH",
    "MAX_CONCURRENT_REQUESTS",
    "REQUEST_TIMEOUT",
    "TOKEN_PATH",
    "TOKEN_REFRESH_MARGIN",
    "AuthError",
    "DaikinAuthError",
    "DaikinOneClient",
    "DaikinOneError",
    "DeviceOfflineError",
    "DeviceSummary",
    "EquipmentStatus",
    "FanCirculate",
    "FanCirculateSpeed",
    "IntegratorAuth",
    "InvalidApiKeyError",
    "InvalidCredentialsError",
    "InvalidRequestError",
    "MalformedResponseError",
    "Mode",
    "ModeLimit",
    "RateLimitedError",
    "ServerError",
    "SystemFan",
    "Thermostat",
    "ThermostatState",
    "TokenExpiredError",
    "TransportError",
    "UnsupportedCapabilityError",
    "error_for",
]
