# Architecture

How the integration is put together, and why each rule exists. Everything below is derived from
Daikin's published API limits and Home Assistant's integration quality scale.

```
custom_components/daikinone/
├── api/                  Transport layer — no Home Assistant imports
│   ├── const.py          Base URL, paths, timeouts, concurrency ceiling
│   ├── exceptions.py     Typed error hierarchy + error_for(status, message, retry_after)
│   ├── models.py         IntEnums, ThermostatState, DeviceSummary, Thermostat
│   ├── auth.py           IntegratorAuth — access-token lifecycle
│   └── client.py         DaikinOneClient — requests, retries, status mapping
├── const.py              Domain constants and limits
├── __init__.py           Entry setup/unload/migration
├── config_flow.py        user / reauth / reconfigure / options
├── coordinator.py        Polling, backoff, writes, verification, device lifecycle
├── entity.py             Shared base entity + dynamic platform setup helper
├── climate.py sensor.py binary_sensor.py switch.py select.py
├── diagnostics.py        Redacted config-entry diagnostics
└── strings.json translations/en.json icons.json quality_scale.yaml manifest.json
```

## Layering

`api/` knows nothing about Home Assistant: it takes an `aiohttp.ClientSession` and raises its own
exceptions. Everything Home Assistant-specific — coordinator, entities, translations of errors —
lives one layer up. That keeps the transport testable on its own and keeps HA's exception types
out of the retry logic.

The session is **injected**, never created: `async_get_clientsession(hass)` is passed into
`DaikinOneClient`, so the integration shares Home Assistant's connection pool and TLS
configuration (quality-scale rule `inject-websession`). `aiohttp` is the only dependency and it is
already bundled with Home Assistant, so `manifest.json` declares `"requirements": []`
(`dependency-transparency`, `async-dependency`).

## Authentication and the token lifecycle

`POST /v1/token` exchanges `{"email", "integratorToken"}` for an access token that lives **900
seconds**. There is no refresh flow — you re-POST.

`IntegratorAuth.async_get_access_token()`:

1. **Fast path, no lock:** if a token is cached and `expires_at - 60 > monotonic()`, return it.
   The 60-second margin means a token never expires mid-request.
2. **Slow path:** take an `asyncio.Lock`, re-check the fast-path condition (another task may have
   refreshed while we waited), then POST **inside the request semaphore** so a token fetch counts
   against Daikin's open-request ceiling like any other call.
3. `invalidate(token)` only clears the cache if the token handed in is still the current one — a
   late 401 from a request that used an already-replaced token cannot throw away a good token.

Token errors map straight to the exception table: `400`/`401` → `InvalidCredentialsError`,
`403` → `InvalidApiKeyError`, a response without `accessToken` → `MalformedResponseError`.

## Concurrency and lock order

Daikin's documented limit is **no more than three open requests**. `DaikinOneClient` owns an
`asyncio.Semaphore(3)`; every HTTP call — including the token POST — acquires a slot.

Locks are always taken in this order and never the reverse:

```
per-thermostat write lock  →  auth lock  →  request semaphore
```

The token is fetched **before** entering the semaphore. That matters when several requests get a
401 at once: each releases its slot, all of them queue on the auth lock, the first refreshes, and
the rest see a replaced token and skip the refresh. Exactly one `POST /v1/token` results, and no
task holds a semaphore slot while waiting for the auth lock, so there is no deadlock.

## Request handling

`_request` runs at most two attempts:

- Get a token (outside the semaphore) → `async with semaphore:` do the request with
  `ClientTimeout(total=30)`.
- `aiohttp.ClientError` / `TimeoutError` → `TransportError`.
- HTTP `401` on the first attempt → `auth.invalidate(token)` and retry once. A second `401` is
  `TokenExpiredError`, so a genuinely bad credential surfaces immediately instead of looping.
- Everything else goes through `error_for(status, message, retry_after)`. A body message of
  `DeviceOfflineException` wins over the status code, because Daikin returns it with an otherwise
  generic status.
- Bodies are read with `resp.text()` + `json.loads` so a non-JSON error page becomes
  `MalformedResponseError` rather than an `aiohttp` decode error. Both the documented plural
  `messages` and the singular `message` keys are read.

Logging is `METHOD path -> status` at DEBUG and nothing else — no bodies, no headers, no tokens.
Exception messages are fixed strings with an error `code`; status codes are allowed, response
content is not.

## Polling

`DaikinOneCoordinator` extends `TimestampDataUpdateCoordinator[dict[str, Thermostat]]`.

- **Interval:** `max(180, options.scan_interval)` plus a fresh `uniform(0, 10)` jitter after every
  successful poll. 180 seconds is Daikin's documented floor (one read per three minutes); the
  options form's `NumberSelector(min=180)` and the `max()` in code both enforce it, so no
  configuration can poll faster. The jitter keeps many installations from synchronising onto the
  same second.
- **Shape:** one `GET /v1/devices` to list locations and thermostats, then the per-thermostat
  `GET /v1/devices/{id}` reads gathered with `return_exceptions=True`. The semaphore keeps the
  fan-out inside the three-request ceiling.
- **Per-device failure isolation:** `DeviceOfflineError`, `ServerError`, `TransportError` and
  `InvalidRequestError` on a single thermostat keep that thermostat's last known state with
  `online=False`, so only its entities go unavailable. The transition is logged at INFO exactly
  once when a thermostat goes offline and once when it comes back (`log-when-unavailable`).
- **Account-level failures:** an auth error raises `ConfigEntryAuthFailed`, which starts Home
  Assistant's reauth flow. Rate limiting and other client errors raise `UpdateFailed`, which marks
  every entity unavailable and logs once.
- **Backoff:** a failure counter drives `UpdateFailed(retry_after=max(min(base * 2**n, 1800),
  Retry-After))` — exponential up to 30 minutes, but never shorter than a `Retry-After` header
  when Daikin sends one. The counter resets on the first success. `retry_after` reschedules the
  next poll; every refresh re-arms the timer from the current interval.

## Writes

Every write goes through the coordinator, under a **per-thermostat `asyncio.Lock`**, so two
service calls to the same thermostat serialise and the second builds its payload from the first's
result.

1. **Start from the latest local snapshot.** `PUT /v1/devices/{id}/msp` requires *all three* of
   `mode`, `heatSetpoint`, `coolSetpoint` — a partial write is not possible. Setting only the heat
   setpoint therefore pushes the cool setpoint up (and vice versa) to preserve `setpointDelta`.
2. **Validate locally.** Range and delta violations raise `ServiceValidationError` with a
   translation key (`setpoint_delta`, `setpoint_out_of_range`,
   `single_setpoint_not_applicable`, `state_unknown`) *before* any request — a rejected write
   costs the user nothing.
3. **One request.** Never a read-modify-write round trip against the API; the snapshot is local.
4. **Optimistic update.** The coordinator replaces the cached state with the requested values and
   sets `schedule_enabled=False`, because Daikin documents that `/msp` turns the schedule and the
   Away state off. Entities update instantly.
5. **Coalesced verification read.** Daikin answers writes with `{"message": "Write sent"}` — the
   value is queued, not applied, and the docs require waiting at least **15 seconds** before
   reading. After each successful write the coordinator cancels any pending verification timer and
   arms a new `async_call_later(hass, 15, …)` that calls `async_refresh()`. Three writes at
   t=0/5/10 produce **one** read at t=25, not three.
6. **Errors** map to `HomeAssistantError(translation_domain=DOMAIN, translation_key=<code>)`, so
   the user sees a translated message rather than a stack trace (`action-exceptions`,
   `exception-translations`).

The timer is cancelled by `async_shutdown` and registered with `entry.async_on_unload`, so
unloading the entry during the 15-second window leaves no lingering timer (the test harness fails
the suite if one survives).

**Accepted race:** a poll in flight when a write lands can briefly overwrite the optimistic state.
It self-corrects within 15 seconds at the verification read. Making that impossible would need a
generation counter on every state; the bounded staleness is not worth it.

## Device lifecycle

- **Dynamic devices:** each platform registers a coordinator listener that keeps a set of known
  thermostat ids and creates entities for ids it has not seen. A thermostat added to your account
  appears at the next poll — no restart, no reload (`dynamic-devices`).
- **Stale devices:** after every successful poll the coordinator diffs the previous id set against
  the current one and calls `device_registry.async_update_device(..., remove_config_entry_id=…)`
  for the ones that vanished. On the first poll after migrating from `ha-daikinone` this also
  clears the orphaned equipment devices, which have no counterpart in the official API
  (`stale-devices`).
- **Manual deletion:** `async_remove_config_entry_device` allows deleting any device whose
  identifier is not a current thermostat id, so anything the automatic sweep misses can be removed
  from the device page.
- **Repair issues:** a `PUT /v1/devices/{id}/fan` rejected as unsupported creates a non-fixable
  warning repair issue explaining that the equipment is VRV/split and that the two fan selects
  should be disabled. A later successful fan write deletes the issue (`repair-issues`).

## Config entry

`entry.runtime_data` holds the coordinator (`runtime-data`); there is no `hass.data` bucket. The
entry's unique id is the **lowercased** email so one account cannot be configured twice, while
`data[email]` keeps the exact capitalisation Daikin's case-sensitive token endpoint requires.

Migration from `ha-daikinone`'s v1 entry sets that unique id, defaults
`entity_uid_schema_version` to `0`, and bumps to version 2 while **keeping the password**. Setup
then raises `ConfigEntryAuthFailed`, so Home Assistant asks for the new credentials; the password
is dropped from `data` only after reauthentication succeeds. See [MIGRATION.md](MIGRATION.md).

## Diagnostics

`diagnostics.py` emits the integration version, API host, redacted entry, base and effective poll
intervals, last update success and time, last error code, seconds remaining on the access token,
and each thermostat's model, firmware, online flag, capability decisions (offered HVAC modes,
emergency-heat availability, whether the fan selects apply) and full state.

`TO_REDACT` covers `email`, `api_key`, `integrator_token`, `password`, `access_token`,
`authorization`, `cookie`, `unique_id`, `title`, `id`, `name`, `location_name` — credentials *and*
the identifiers that would let someone correlate a bug report with an account. A test asserts none
of the test credentials or identifiers appear in `json.dumps` of the output, which is why the
diagnostics file is safe to attach to an issue.

## Testing

Every test runs against `aioclient_mock` — no live credentials, no network. Time is controlled
with the `freezer` fixture plus `async_fire_time_changed`, never real sleeps. Beyond the
per-module tests, four suites guard the contracts described above:

- `tests/test_api_spec_conformance.py` replays every request the client can make against
  `tests/spec/daikin_open_api.json`, a checked-in transcription of the official documentation, and
  checks paths, headers, request body key sets, enum tables and the documented limits.
- `tests/test_no_legacy_api.py` fails if any private hostname or endpoint appears in runtime code.
- `tests/test_quality_scale.py` validates `quality_scale.yaml` against hassfest's rule list and
  checks that the README covers every `docs-*` rule.
- `tests/test_repo_metadata.py` checks the manifest/HACS metadata and scans every tracked text
  file for credential-shaped material -- opaque 100+ character blobs (the shape of the RSA-OAEP
  JWE Daikin issues as an Integrator Token), 40+ character hex strings and literal `Bearer`
  tokens -- plus any `password` field in a fixture. This is the enforced gate: gitleaks' JWT rule
  cannot match a JWE, and `scripts/secret_scan.sh`'s `.env` comparison and gitleaks pass are both
  skipped on a CI runner (the script now says so instead of reporting them clean).

`scripts/check_spec_drift.py` (network, not part of pytest) diffs the live documentation pages
against the snapshot, so a change on Daikin's side is a build signal rather than a surprise.
`scripts/live_check.py` runs a read-only verification against a real account from `.env.test` and
prints a fully redacted summary.
