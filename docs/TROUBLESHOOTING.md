# Troubleshooting

Start with the [README's troubleshooting table](../README.md#troubleshooting) for the quick
answers. This document has the detail: what each error key means, how to work through unavailable
entities, and how to collect diagnostics.

## Config flow error codes

Every message the setup, reauthentication and reconfiguration forms can show, and what to do about
it. The HTTP status behind each one comes from Daikin's documented error table.

| Shown as | Error key | HTTP | Meaning | Fix |
| --- | --- | --- | --- | --- |
| Invalid authentication | `invalid_auth` | 400 / 401 | The **Integrator Token** was rejected, or the email does not match the account. Daikin returns 401 with `NotAuthorizedException` when the access token cannot be issued. | Check the email's capitalisation first — it is case-sensitive and a mismatch looks exactly like a bad token. Then issue a fresh token: SkyportHome → SkyportCare → home integration → get integration token. |
| Invalid API key | `invalid_api_key` | 403 | The `x-api-key` header value is not accepted. | Re-copy the API key from SkyportCare → home integration → **developer**. A very long value with dots in it is the Integrator Token, not the API key — you have pasted them into the wrong fields. |
| Too many requests | `rate_limited` | 429 | Daikin is rate-limiting this key. | Wait several minutes. Check whether another integration, a script, or a second Home Assistant instance is using the same key. |
| No devices found | `no_devices` | 200 | Authentication worked but `GET /v1/devices` returned an empty list. | Confirm you used the **home owner's** account. Guest/shared accounts can log in but own no devices. |
| Failed to connect | `cannot_connect` | 415 / 404 / 5xx / timeout / non-JSON | Network failure, a Daikin server error, or an unparseable response. | Retry. If it persists for more than a few minutes, check <https://status.daikinone.com> (or the app) and open an issue with diagnostics. |
| Unexpected error | `unknown` | — | Something the integration did not anticipate. | Enable debug logging (below) and open an issue with the log and diagnostics. |
| Already configured | `already_configured` (abort) | — | This email is already set up. | Use **Reconfigure** on the existing entry instead of adding a second one. |
| Wrong account | `wrong_account` (abort) | — | During reauthentication or reconfiguration you entered a *different* email than the entry was created with. | Re-enter the original email. To switch accounts, delete the entry and add a new one — swapping accounts in place would silently reassign every entity. |

## Runtime errors on service calls

These appear as a red toast when an action fails, and in the log.

| Translation key | Meaning | Fix |
| --- | --- | --- |
| `auth_failed` | Credentials stopped working mid-session. | Complete the reauthentication prompt on the integration entry. |
| `rate_limited` | Daikin returned 429 for the write. | Retry in a few minutes; reduce how often automations write. |
| `device_offline` | The thermostat is not reachable by Daikin (`DeviceOfflineException`). | Check the thermostat's own network connection. |
| `invalid_request` | Daikin rejected the request body (400/415). | Usually a setpoint outside the thermostat's limits. Check min/max on the climate entity. Report it if the values look valid. |
| `unsupported_capability` | A fan write was rejected — the equipment is VRV (P1P2) or single/multi-split (S21). | Disable the two fan select entities; see [Fan controls](#fan-controls-on-vrv-and-splitmini-split-systems). |
| `write_failed` | Transport failure, server error, or an unparseable response. | Retry. Persistent failures need diagnostics. |
| `setpoint_delta` | Heat and cool setpoints are closer together than the thermostat's required delta. | Move them apart by at least the delta the thermostat reports. |
| `setpoint_out_of_range` | A setpoint is outside the thermostat's own minimum/maximum. | Use a value inside the range shown on the climate card. |
| `single_setpoint_not_applicable` | A single target temperature was sent while the thermostat is in auto/heat_cool. | Send a temperature **range** (low and high) in auto mode. |
| `state_unknown` | The integration has no state for that thermostat yet (first poll has not completed, or the device is offline). | Wait for the next poll; if the device is offline, fix that first. |

## Reauthentication walkthrough

Home Assistant starts this automatically when polling gets a 401 or 403, and after migrating from
`ha-daikinone`.

1. A **Reauthentication required** notification appears, and the entry shows a repair/attention
   badge on **Settings → Devices & Services**.
2. Click it (or **Reconfigure** on the entry). The form asks for three fields; the email is
   pre-filled from the entry.
3. **Email** — correct the capitalisation if needed; it must match the account exactly.
4. **API key** — the short opaque value from the developer menu.
5. **Integrator Token** — the long JWE. Paste all of it; it is normal for the field to look like it
   contains gibberish.
6. Submit. The integration requests a token and lists your devices before accepting the values. On
   success the entry reloads and any password left over from `ha-daikinone` is deleted from
   storage.
7. On failure the form comes back with one of the error keys above and **nothing is changed** —
   your old configuration is still intact, so you can retry.

Reauthentication never creates a second entry and never changes which devices belong to it.

## Rate limiting and backoff

Daikin publishes three usage limits, and the integration obeys all three:

| Limit | Documented value | How it is enforced |
| --- | --- | --- |
| Poll rate | not faster than once per 3 minutes | Interval floor of 180 s in both the options form and the coordinator, plus up to 10 s of jitter |
| Open requests | no more than 3 | A semaphore of 3 in the API client covers every call, token requests included |
| Read-after-write | wait at least 15 seconds | A single coalesced verification read 15 s after the last successful write |

When a 429 does arrive:

- The coordinator marks the update failed (entities go unavailable) and logs once, not every cycle.
- The next poll is delayed by an exponentially growing backoff — the base interval doubled per
  consecutive failure, capped at 30 minutes.
- If Daikin sends a `Retry-After` header, the longer of the two values is used.
- The first successful poll resets the backoff to the normal interval.

Nothing needs to be done manually. If 429s are constant, something else is sharing the API key.

## "Entities are unavailable" decision tree

```
Are ALL Daikin entities unavailable?
├── YES → account-level failure
│   ├── Entry shows "Reauthentication required"?
│   │      → credentials rejected (401/403). Reauthenticate.
│   ├── Log shows "Too Many Requests" / 429?
│   │      → rate limited. Wait; backoff recovers automatically.
│   ├── Log shows timeouts / cannot connect / 5xx?
│   │      → Home Assistant cannot reach Daikin, or Daikin is down.
│   │        Check internet access from the HA host, then Daikin's status.
│   └── Nothing in the log and last update is recent?
│          → the entry may be disabled or not loaded. Reload the entry.
│
└── NO → one thermostat only
    ├── Its connectivity binary sensor is available and OFF?
    │      → the thermostat is offline at Daikin. Check its Wi-Fi,
    │        power-cycle it, confirm it is online in the Daikin app.
    ├── The device disappeared entirely?
    │      → it was removed from your Daikin account; the integration
    │        removes vanished devices at the next successful poll.
    └── Only SOME entities on the device are missing?
           → they are disabled by default (the four equipment-status
             binary sensors). Enable them from the device page.
```

The connectivity binary sensor is deliberately **kept available** while its thermostat is offline —
otherwise you could not write an automation that alerts on it.

## Fan controls on VRV and split/mini-split systems

The official API has **no capability field**: nothing in any response says whether a system
supports fan circulation. Daikin's documentation only states that VRV (P1P2) and single/multi-split
(S21) equipment ignore the fan endpoint and run the fan at maximum speed.

So the two selects — **Fan circulation** and **Fan circulation speed** — are created for every
thermostat and enabled by default. They are inert until you use them: the integration never writes
to `/fan` during polling, only on an explicit selection.

If a write is rejected:

1. The action fails with `unsupported_capability` and the select's state does not change.
2. A repair issue appears under **Settings → Repairs**, naming the thermostat.
3. Disable the two entities: device page → the entity → gear icon → **Enabled** off.

The repair issue disappears on its own if a later fan write succeeds. Nothing else about the
integration is affected — mode, setpoints and the schedule switch all work normally on VRV and
split systems.

## Debug logging

Add to `configuration.yaml` and restart, or use **Settings → Devices & Services → Daikin One →
Enable debug logging**:

```yaml
logger:
  default: warning
  logs:
    custom_components.daikinone: debug
```

Debug output contains request lines of the form `GET /v1/devices -> 200` and coordinator state
transitions. **It never contains tokens, headers, request bodies or response bodies**, so a debug
log is safe to share — but skim it anyway before posting.

## Downloading diagnostics

1. **Settings → Devices & Services → Daikin One**.
2. Click the device (or the entry's three-dot menu) → **Download diagnostics**.
3. A JSON file is saved locally.

It contains the integration version, the API host, the redacted config entry, base and effective
polling intervals, whether the last update succeeded and when, the last error code, the seconds
left on the access token, and per-thermostat model, firmware, online state, the capability
decisions the integration made (which HVAC modes it offers, whether emergency heat is available,
whether the fan selects apply) and the full thermostat state.

Redacted to `**REDACTED**` before the file is written: email, API key, Integrator Token, any stored
password, access token, `Authorization` and `Cookie` headers, config entry unique id and title, and
every device id, device name and location name. A test asserts that none of these appear anywhere
in the serialised output.

**The diagnostics file is safe to attach to a GitHub issue** — please do; it answers most of the
questions a maintainer would otherwise have to ask.

## Reporting a problem

Include:

- What you did and what happened instead.
- The diagnostics file.
- The relevant log lines (debug logging enabled if possible).
- Your thermostat model and whether the system is unitary, VRV, or a split/mini-split.
- Home Assistant version and how the integration was installed (HACS or manual).

Do **not** include your API key, Integrator Token, account email or password. Nothing in a bug
report ever needs them, and posting one means revoking and reissuing it.
