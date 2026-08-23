"""Strict conformance to the official Daikin One Open API specification.

Everything here is parametrised from ``tests/spec/daikin_open_api.json`` (a hand-transcribed
snapshot of the official docs, kept honest by ``scripts/check_spec_drift.py``). Adding a row to
that snapshot adds a test case; a mapping that does not cover the snapshot fails loudly rather
than silently skipping. If Daikin changes the API, the drift script goes red first and these
tests go red as soon as the snapshot is updated.

Imports of the transport layer (``api.client`` / ``api.exceptions`` / ``api.const``) are made
inside the tests that need them, so that the enum/limit/model conformance still runs when the
transport modules are mid-flight.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from aiohttp.payload import JsonPayload
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import load_json_object_fixture
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone import const as ha_const
from custom_components.daikinone.api import models
from tests.conftest import API_KEY, DEVICE_IDS, DEVICES_URL, EMAIL, INTEGRATOR_TOKEN, TOKEN_URL, calls

SPEC: dict[str, Any] = json.loads((Path(__file__).parent / "spec" / "daikin_open_api.json").read_text(encoding="utf-8"))
BASE_URL: str = SPEC["base_url"]
ENDPOINTS: dict[str, Any] = SPEC["endpoints"]
DEVICE_ID = DEVICE_IDS[0]
README = Path(__file__).parent.parent / "README.md"
# Every GET /v1/devices/{id} payload printed in the docs (both must parse identically).
DEVICE_EXAMPLES = sorted(name for name in SPEC["examples"] if name.startswith("device_response"))

# Spec enum table name -> the IntEnum that must reproduce it exactly.
ENUM_CLASSES: dict[str, type[models.Mode] | Any] = {
    "mode": models.Mode,
    "modeLimit": models.ModeLimit,
    "equipmentStatus": models.EquipmentStatus,
    "fan": models.SystemFan,
    "fanCirculate": models.FanCirculate,
    "fanCirculateSpeed": models.FanCirculateSpeed,
}

# Documented GET /v1/devices/{id} field -> ThermostatState attribute it must populate.
STATE_ATTRS = {
    "equipmentStatus": "equipment_status",
    "mode": "mode",
    "modeLimit": "mode_limit",
    "modeEmHeatAvailable": "em_heat_available",
    "fan": "fan",
    "fanCirculate": "fan_circulate",
    "fanCirculateSpeed": "fan_circulate_speed",
    "heatSetpoint": "heat_setpoint",
    "coolSetpoint": "cool_setpoint",
    "setpointDelta": "setpoint_delta",
    "setpointMinimum": "setpoint_minimum",
    "setpointMaximum": "setpoint_maximum",
    "tempIndoor": "temp_indoor",
    "humIndoor": "hum_indoor",
    "tempOutdoor": "temp_outdoor",
    "humOutdoor": "hum_outdoor",
    "scheduleEnabled": "schedule_enabled",
    "geofencingEnabled": "geofencing_enabled",
}

# Documented GET /v1/devices field -> DeviceSummary attribute it must populate.
SUMMARY_ATTRS = {"id": "id", "name": "name", "model": "model", "firmwareVersion": "firmware_version"}


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> Any:
    """The real client, bound to HA's shared session (intercepted by aioclient_mock)."""
    from custom_components.daikinone.api import DaikinOneClient  # noqa: PLC0415

    return DaikinOneClient(async_get_clientsession(hass), EMAIL, API_KEY, INTEGRATOR_TOKEN)


def _body(data: Any) -> dict[str, Any]:
    """Return the request body as a dict, whether the client sent json= or a serialised str."""
    if data is None:
        return {}
    if isinstance(data, (str, bytes)):
        parsed: dict[str, Any] = json.loads(data)
        return parsed
    return dict(data)


def _check_type(key: str, value: Any) -> None:
    """Assert a request value matches the documented type for that field."""
    kind = SPEC["field_types"].get(key, "string")
    # bool is a subclass of int, so an int/float field must reject it explicitly.
    checks = {
        "bool": isinstance(value, bool),
        "int": isinstance(value, int) and not isinstance(value, bool),
        "int_or_bool": isinstance(value, int),
        "float": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str) and bool(value),
    }
    assert checks[kind], f"{key}={value!r} does not match the documented type {kind!r}"


async def _invoke(client: Any, endpoint: str) -> None:
    """Make the one client call that exercises the given documented endpoint."""
    if endpoint in ("token", "devices"):
        # The token POST is issued implicitly before the first authorized request.
        await client.async_get_devices()
    elif endpoint == "device":
        await client.async_get_thermostat(DEVICE_ID)
    elif endpoint == "msp":
        await client.async_set_mode_setpoints(DEVICE_ID, models.Mode.HEAT, 20.5, 23.5)
    elif endpoint == "schedule":
        await client.async_set_schedule_enabled(DEVICE_ID, False)
    elif endpoint == "fan":
        await client.async_set_fan(DEVICE_ID, models.FanCirculate.SCHEDULE, models.FanCirculateSpeed.LOW)
    else:
        pytest.fail(f"spec endpoint {endpoint!r} has no client call in this test - add one")


# --------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------


def test_enum_map_covers_every_spec_table() -> None:
    """Every documented enum table is checked (a new table in the snapshot must be mapped)."""
    assert set(ENUM_CLASSES) == set(SPEC["enums"])


@pytest.mark.parametrize("enum_name", sorted(SPEC["enums"]), ids=str)
def test_enum_matches_spec_table_exactly(enum_name: str) -> None:
    """The IntEnum has exactly the documented value->name pairs, plus UNKNOWN=-1 and nothing else."""
    cls = ENUM_CLASSES[enum_name]
    documented = {int(value): re.sub(r"[ -]", "_", name) for value, name in SPEC["enums"][enum_name].items()}

    actual = {int(member): member.name.lower() for member in cls if member.name != "UNKNOWN"}

    assert actual == documented
    assert cls.UNKNOWN.value == -1
    assert set(cls) == {cls(value) for value in documented} | {cls.UNKNOWN}


@pytest.mark.parametrize("enum_name", sorted(SPEC["enums"]), ids=str)
def test_enum_maps_undocumented_values_to_unknown(enum_name: str) -> None:
    """An integer Daikin has not documented resolves to UNKNOWN instead of raising."""
    cls = ENUM_CLASSES[enum_name]
    unseen = max(int(value) for value in SPEC["enums"][enum_name]) + 1

    assert cls(unseen) is cls.UNKNOWN


# --------------------------------------------------------------------------------------
# Documented usage limits
# --------------------------------------------------------------------------------------


def test_poll_interval_floor_matches_spec() -> None:
    """Daikin: "Do not poll at an interval quicker than once every 3 minutes"."""
    documented = SPEC["limits"]["poll_min_seconds"]

    assert documented == ha_const.MIN_SCAN_INTERVAL


def test_verify_delay_respects_write_settle_time() -> None:
    """Daikin: "Please wait a minimum of 15 seconds for successful changes to be reflected"."""
    documented = SPEC["limits"]["write_settle_seconds"]

    assert documented <= ha_const.VERIFY_DELAY_SECONDS


def test_api_constants_match_spec() -> None:
    """Base URL, paths, concurrency ceiling and token margin all come from the spec."""
    from custom_components.daikinone.api import const as api_const  # noqa: PLC0415

    token_path = ENDPOINTS["token"]["path"]
    devices_path = ENDPOINTS["devices"]["path"]
    max_open = SPEC["limits"]["max_open_requests"]

    assert api_const.API_BASE_URL == BASE_URL
    assert token_path == api_const.TOKEN_PATH
    assert devices_path == api_const.DEVICES_PATH
    assert max_open == api_const.MAX_CONCURRENT_REQUESTS
    assert 0 < api_const.TOKEN_REFRESH_MARGIN < ENDPOINTS["token"]["expires_in_example"]


# --------------------------------------------------------------------------------------
# Request conformance: URL, method, headers, body keys and value types
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS), ids=str)
async def test_request_conformance(client: Any, mock_api: AiohttpClientMocker, endpoint: str) -> None:
    """Each documented endpoint is called exactly as the docs describe it."""
    spec = ENDPOINTS[endpoint]
    expected_url = BASE_URL + spec["path"].replace("{id}", DEVICE_ID)

    await _invoke(client, endpoint)

    matches = [
        call
        for call in mock_api.mock_calls
        if call[0].lower() == spec["method"].lower() and str(call[1]) == expected_url
    ]
    assert len(matches) == 1, f"expected exactly one {spec['method']} {expected_url}, got {len(matches)}"
    _method, _url, data, raw_headers = matches[0]

    headers = {key.lower(): value for key, value in dict(raw_headers or {}).items()}
    assert headers.get("x-api-key") == API_KEY
    if spec["requires_authorization_header"]:
        assert headers.get("authorization", "").startswith(f"{SPEC['headers']['authorization_scheme']} ")
    else:
        assert "authorization" not in headers

    if "request_keys" in spec:
        # The docs require Content-Type: application/json on every body (415 otherwise).
        # AiohttpClientMocker records only the headers the caller passed explicitly, and a body
        # handed to aiohttp as json= is recorded as a dict -- that path carries the header
        # implicitly (pinned by test_json_bodies_carry_the_documented_content_type). Any other
        # body is serialised by the client itself and must therefore set the header itself.
        content_type = headers.get("content-type")
        if content_type is None:
            assert isinstance(data, dict), f"{endpoint} serialises its own body and must set Content-Type"
        else:
            assert content_type == SPEC["headers"]["content_type"]
        body = _body(data)
        assert set(body) == set(spec["request_keys"]), f"{endpoint} body keys must equal the documented set"
        for key, value in body.items():
            _check_type(key, value)
    else:
        assert _body(data) == {}, f"{spec['method']} {expected_url} must not carry a body"


def test_json_bodies_carry_the_documented_content_type() -> None:
    """Closes the loop on the one header AiohttpClientMocker cannot record.

    A body passed as ``json=`` makes aiohttp attach ``Content-Type: application/json``
    (``JsonPayload``'s own content type) -- exactly what the docs demand, since a request
    without it is answered with 415. ``test_request_conformance`` proves every documented
    request body goes out that way; this proves that way still means the documented type.
    """
    assert JsonPayload({}).content_type == SPEC["headers"]["content_type"]


async def test_token_response_example_is_consumed_verbatim(client: Any, aioclient_mock: AiohttpClientMocker) -> None:
    """All three documented token-response keys are consumed: token, expiry and type."""
    example = SPEC["examples"]["token_response"]
    assert set(example) == set(ENDPOINTS["token"]["response_keys"])
    aioclient_mock.post(TOKEN_URL, json=example)
    aioclient_mock.get(DEVICES_URL, json=[])

    assert await client.async_get_devices() == []

    authorization = calls(aioclient_mock, "get", "/v1/devices")[0][3]["Authorization"]
    assert authorization == f"{example['tokenType']} {example['accessToken']}"
    assert client.auth.expires_in == pytest.approx(example["accessTokenExpiresIn"], abs=1)


async def test_write_success_message_is_not_an_error(client: Any, aioclient_mock: AiohttpClientMocker) -> None:
    """A write answered with the documented success body completes without raising."""
    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.put(f"{DEVICES_URL}/{DEVICE_ID}/schedule", json={"message": SPEC["write_success_message"]})

    await client.async_set_schedule_enabled(DEVICE_ID, True)

    assert len(calls(aioclient_mock, "put", f"/v1/devices/{DEVICE_ID}/schedule")) == 1


# --------------------------------------------------------------------------------------
# Documented status codes and messages
# --------------------------------------------------------------------------------------


def _status_families() -> dict[str, Any]:
    from custom_components.daikinone.api import exceptions as exc  # noqa: PLC0415

    return {
        "400": exc.InvalidRequestError,
        # 401 (accessToken) and 403 (integratorApiKey) must stay distinguishable: they blame
        # different credentials, and the coordinator/config flow act on the difference.
        "401": exc.TokenExpiredError,
        "403": exc.InvalidApiKeyError,
        "415": exc.InvalidRequestError,
        "429": exc.RateLimitedError,
        "500": exc.ServerError,
    }


@pytest.mark.parametrize("status", sorted(code for code in SPEC["status_codes"] if code != "200"), ids=str)
def test_documented_status_maps_to_exception(status: str) -> None:
    """Every documented failure status maps to the exception family from the plan's table."""
    from custom_components.daikinone.api import exceptions as exc  # noqa: PLC0415

    families = _status_families()
    assert status in families, f"documented status {status} has no exception mapping"

    error = exc.error_for(int(status), None, None)

    assert isinstance(error, families[status]), f"{status} -> {type(error).__name__}"
    assert isinstance(error, exc.DaikinOneError)
    assert error.code


def test_rate_limited_carries_retry_after() -> None:
    """HTTP 429 keeps the Retry-After hint so the coordinator can reschedule."""
    from custom_components.daikinone.api import exceptions as exc  # noqa: PLC0415

    assert exc.error_for(429, None, 600).retry_after == 600
    assert exc.error_for(429, None, None).retry_after is None


@pytest.mark.parametrize("status", [400, 401, 403, 415, 429, 500])
def test_device_offline_message_wins_over_status(status: int) -> None:
    """The "DeviceOfflineException" message identifies an offline thermostat, whatever status carries it."""
    from custom_components.daikinone.api import exceptions as exc  # noqa: PLC0415

    assert isinstance(exc.error_for(status, "DeviceOfflineException", None), exc.DeviceOfflineError)


@pytest.mark.parametrize("message", SPEC["documented_messages"], ids=str)
def test_documented_messages_produce_daikin_errors(message: str) -> None:
    """Every message string in the docs is handled (no crash, no bare Exception)."""
    from custom_components.daikinone.api import exceptions as exc  # noqa: PLC0415

    error = exc.error_for(400, message, None)

    assert isinstance(error, exc.DaikinOneError)
    assert error.code


@pytest.mark.parametrize("body_key", SPEC["error_body_keys"], ids=str)
async def test_error_body_keys_are_both_honoured(
    client: Any, aioclient_mock: AiohttpClientMocker, body_key: str
) -> None:
    """The docs print plural "messages" for errors and singular "message" for writes; read both."""
    from custom_components.daikinone.api import exceptions as exc  # noqa: PLC0415

    aioclient_mock.post(TOKEN_URL, json=load_json_object_fixture("token.json"))
    aioclient_mock.get(f"{DEVICES_URL}/{DEVICE_ID}", status=400, json={body_key: "DeviceOfflineException"})

    with pytest.raises(exc.DeviceOfflineError):
        await client.async_get_thermostat(DEVICE_ID)


# --------------------------------------------------------------------------------------
# Documented response fields are all consumed by the models
# --------------------------------------------------------------------------------------


def test_state_attribute_map_covers_documented_response() -> None:
    """Every documented GET /v1/devices/{id} field has a model attribute."""
    assert set(STATE_ATTRS) == set(ENDPOINTS["device"]["response_keys"])


@pytest.mark.parametrize("example", DEVICE_EXAMPLES, ids=str)
@pytest.mark.parametrize("field", sorted(ENDPOINTS["device"]["response_keys"]), ids=str)
def test_every_documented_field_is_consumed(field: str, example: str) -> None:
    """A documented example populates every documented field with its documented value/type."""
    state = models.ThermostatState.from_json(SPEC["examples"][example])
    documented = SPEC["examples"][example][field]

    actual = getattr(state, STATE_ATTRS[field])

    assert actual is not None
    if field in ENUM_CLASSES:
        # equipmentStatus 0 appears in the docs' schema stub with no documented meaning -> UNKNOWN.
        assert actual is ENUM_CLASSES[field](int(documented))
    elif SPEC["field_types"][field] in ("bool", "int_or_bool"):
        assert actual is bool(documented)
    else:
        _check_type(field, actual)
        assert actual == pytest.approx(float(documented))


def test_device_list_keys_are_consumed() -> None:
    """locationName plus the four documented device keys all reach DeviceSummary."""
    assert set(SUMMARY_ATTRS) == set(ENDPOINTS["devices"]["device_keys"])
    assert ENDPOINTS["devices"]["location_keys"] == ["locationName", "devices"]

    device = {"id": "1abcdef2", "name": "Main Room", "model": "ONEPLUS", "firmwareVersion": "2.3.5"}
    summary = models.DeviceSummary.from_json({"locationName": "Country House"}, device)

    assert summary is not None
    assert summary.location_name == "Country House"
    for key, attribute in SUMMARY_ATTRS.items():
        assert getattr(summary, attribute) == device[key]


@pytest.mark.parametrize("model", ENDPOINTS["devices"]["observed_models"], ids=str)
def test_observed_models_are_passed_through(model: str) -> None:
    """Model strings are surfaced verbatim (never mapped to a closed set)."""
    summary = models.DeviceSummary.from_json({}, {"id": "dev1", "name": "T", "model": model})

    assert summary is not None
    assert summary.model == model


# --------------------------------------------------------------------------------------
# Documentation guard
# --------------------------------------------------------------------------------------


def test_readme_documents_the_api_contract() -> None:
    """The README states the host, the credential names and Daikin's usage limits."""
    assert README.is_file(), "README.md is missing"
    text = README.read_text(encoding="utf-8").lower()

    assert BASE_URL.lower() in text
    for needle in ("integrator token", "api key", "180", "15 second"):
        assert needle in text, f"README must mention {needle!r}"
    assert any(phrase in text for phrase in ("three concurrent", "3 concurrent", "more than 3")), (
        "README must state Daikin's 3-open-request limit"
    )
