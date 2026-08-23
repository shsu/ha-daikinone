"""Tests for the Integrator API token flow (custom_components/daikinone/api/auth.py).

Every assertion here is about the wire: what the client sends to POST /v1/token, how often
it sends it, and that no credential ever reaches an exception message or a log record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import (
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.daikinone.api import (
    DaikinOneClient,
    DaikinOneError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    MalformedResponseError,
    ServerError,
    TokenExpiredError,
    TransportError,
)
from tests.conftest import (
    ACCESS_TOKEN,
    API_KEY,
    DEVICES_URL,
    EMAIL,
    INTEGRATOR_TOKEN,
    TOKEN_URL,
    calls,
    gated,
    sequence,
)

# The margin at which the client refreshes proactively: 900 s lifetime - 60 s margin.
REFRESH_AFTER = 840

SECRETS = (API_KEY, INTEGRATOR_TOKEN, ACCESS_TOKEN)


@pytest.fixture
def client(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> DaikinOneClient:
    """A client bound to HA's shared session (which aioclient_mock intercepts)."""
    return DaikinOneClient(async_get_clientsession(hass), EMAIL, API_KEY, INTEGRATOR_TOKEN)


def _token_json() -> dict[str, Any]:
    return dict(load_json_object_fixture("token.json"))


def _devices_json() -> list[Any]:
    return list(load_json_array_fixture("devices.json"))


async def test_token_request_and_authorized_call(client: DaikinOneClient, mock_api: AiohttpClientMocker) -> None:
    """POST /v1/token carries exactly the documented body and no Authorization header."""
    await client.async_get_devices()

    posts = calls(mock_api, "post", "/v1/token")
    assert len(posts) == 1
    _method, _url, body, headers = posts[0]
    assert body == {"email": EMAIL, "integratorToken": INTEGRATOR_TOKEN}
    assert headers["x-api-key"] == API_KEY
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers
    assert "authorization" not in {k.lower() for k in headers}

    gets = calls(mock_api, "get", "/v1/devices")
    assert len(gets) == 1
    assert gets[0][3]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert gets[0][3]["x-api-key"] == API_KEY


async def test_token_is_cached_between_calls(client: DaikinOneClient, mock_api: AiohttpClientMocker) -> None:
    """Two API calls in a row share one access token."""
    assert client.auth.expires_in is None

    await client.async_get_devices()
    await client.async_get_devices()

    assert len(calls(mock_api, "post", "/v1/token")) == 1
    assert len(calls(mock_api, "get", "/v1/devices")) == 2
    assert client.auth.expires_in is not None
    assert 0 < client.auth.expires_in <= 900


async def test_invalidate_ignores_a_token_that_is_no_longer_current(
    client: DaikinOneClient, mock_api: AiohttpClientMocker
) -> None:
    """A late 401 for a superseded token must not throw away the token that replaced it."""
    await client.async_get_devices()

    client.auth.invalidate("a-token-that-was-already-replaced")
    await client.async_get_devices()
    assert len(calls(mock_api, "post", "/v1/token")) == 1

    client.auth.invalidate(ACCESS_TOKEN)
    assert client.auth.expires_in is None
    await client.async_get_devices()
    assert len(calls(mock_api, "post", "/v1/token")) == 2


async def test_token_refreshed_proactively(
    client: DaikinOneClient, mock_api: AiohttpClientMocker, freezer: Any
) -> None:
    """The token is reused until 60 s before expiry, then refreshed without a 401."""
    await client.async_get_devices()

    freezer.tick(REFRESH_AFTER - 1)
    await client.async_get_devices()
    assert len(calls(mock_api, "post", "/v1/token")) == 1

    freezer.tick(2)
    await client.async_get_devices()
    assert len(calls(mock_api, "post", "/v1/token")) == 2


async def test_concurrent_cold_start_issues_one_token(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Three simultaneous cold requests coalesce into a single POST /v1/token."""
    counter = {"in_flight": 0, "peak": 0}
    aioclient_mock.post(TOKEN_URL, side_effect=gated(_token_json(), counter))
    aioclient_mock.get(DEVICES_URL, json=_devices_json())

    results = await asyncio.gather(*(client.async_get_devices() for _ in range(3)))

    assert all(len(r) == 3 for r in results)
    assert len(calls(aioclient_mock, "post", "/v1/token")) == 1
    assert len(calls(aioclient_mock, "get", "/v1/devices")) == 3


async def test_401_refreshes_token_and_retries_once(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A single 401 triggers exactly one refresh and one retry, then succeeds."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(
        DEVICES_URL,
        side_effect=sequence(
            {"status": 401, "json": {"messages": "NotAuthorizedException"}},
            {"status": 200, "json": _devices_json()},
        ),
    )

    devices = await client.async_get_devices()

    assert len(devices) == 3
    assert len(calls(aioclient_mock, "post", "/v1/token")) == 2
    assert len(calls(aioclient_mock, "get", "/v1/devices")) == 2


async def test_persistent_401_raises_without_looping(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A permanently rejected token gives up after one retry (2 tokens, 2 reads)."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, status=401, json={"messages": "NotAuthorizedException"})

    with pytest.raises(TokenExpiredError):
        await client.async_get_devices()

    assert len(calls(aioclient_mock, "post", "/v1/token")) == 2
    assert len(calls(aioclient_mock, "get", "/v1/devices")) == 2


@pytest.mark.parametrize("status", [400, 401])
async def test_token_endpoint_rejection_is_invalid_credentials(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """400/401 on the token endpoint means the email or integrator token is wrong."""
    aioclient_mock.post(TOKEN_URL, status=status, json={"messages": "NotAuthorizedException"})

    with pytest.raises(InvalidCredentialsError):
        await client.async_get_devices()

    assert len(calls(aioclient_mock, "get", "/v1/devices")) == 0


async def test_token_endpoint_403_is_invalid_api_key(
    client: DaikinOneClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """403 on the token endpoint means the API key is wrong."""
    aioclient_mock.post(TOKEN_URL, status=403, json={"messages": "NotAuthorizedException"})

    with pytest.raises(InvalidApiKeyError):
        await client.async_get_devices()


async def test_403_on_data_call_is_not_retried(client: DaikinOneClient, aioclient_mock: AiohttpClientMocker) -> None:
    """A bad API key on a data call fails immediately: no token refresh, no retry."""
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, status=403, json={"messages": "NotAuthorizedException"})

    with pytest.raises(InvalidApiKeyError):
        await client.async_get_devices()

    assert len(calls(aioclient_mock, "get", "/v1/devices")) == 1
    assert len(calls(aioclient_mock, "post", "/v1/token")) == 1


async def test_long_token_and_mixed_case_email_sent_verbatim(
    client: DaikinOneClient, mock_api: AiohttpClientMocker
) -> None:
    """A >2000-char JWE integrator token and a mixed-case email survive byte-for-byte."""
    await client.async_get_devices()

    body = calls(mock_api, "post", "/v1/token")[0][2]
    assert len(INTEGRATOR_TOKEN) > 2000
    assert body["integratorToken"] == INTEGRATOR_TOKEN
    assert body["email"] == EMAIL
    assert body["email"] != EMAIL.lower()


async def test_secrets_never_reach_exceptions_or_logs(
    hass: HomeAssistant,
    client: DaikinOneClient,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Credentials appear in no exception string and in no log record, even at DEBUG."""
    caplog.set_level(logging.DEBUG)

    # The API echoes secret-looking material back in the error body; none of it may escape.
    aioclient_mock.post(TOKEN_URL, json=_token_json())
    aioclient_mock.get(DEVICES_URL, status=401, json={"messages": ACCESS_TOKEN})
    with pytest.raises(TokenExpiredError) as auth_err:
        await client.async_get_devices()

    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, status=403, json={"messages": API_KEY})
    fresh = DaikinOneClient(async_get_clientsession(hass), EMAIL, API_KEY, INTEGRATOR_TOKEN)
    with pytest.raises(InvalidApiKeyError) as key_err:
        await fresh.async_get_devices()

    for exc in (auth_err.value, key_err.value):
        for secret in SECRETS:
            assert secret not in str(exc)
            assert secret not in repr(exc)

    assert "GET /v1/devices -> 401" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"exc": aiohttp.ClientError()}, TransportError),
        ({"status": 500}, ServerError),
        ({"text": "<html>gateway</html>"}, MalformedResponseError),
        ({"json": ["not", "an", "object"]}, MalformedResponseError),
        ({"json": {"accessTokenExpiresIn": 900}}, MalformedResponseError),
        ({"json": {"accessToken": "", "accessTokenExpiresIn": 900}}, MalformedResponseError),
        ({"json": {"accessToken": ACCESS_TOKEN}}, MalformedResponseError),
        ({"json": {"accessToken": ACCESS_TOKEN, "accessTokenExpiresIn": True}}, MalformedResponseError),
    ],
    ids=[
        "transport",
        "server-error",
        "not-json",
        "not-an-object",
        "no-token",
        "empty-token",
        "no-expiry",
        "bool-expiry",
    ],
)
async def test_unusable_token_responses(
    client: DaikinOneClient,
    aioclient_mock: AiohttpClientMocker,
    response: dict[str, Any],
    expected: type[DaikinOneError],
) -> None:
    """Anything other than the documented token payload fails loudly, never silently."""
    aioclient_mock.post(TOKEN_URL, **response)

    with pytest.raises(expected):
        await client.async_get_devices()

    assert client.auth.expires_in is None
    assert len(calls(aioclient_mock, "get", "/v1/devices")) == 0
