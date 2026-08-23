# Migration guide

Two starting points are covered:

- [From `zlangbert/ha-daikinone`](#from-zlangbertha-daikinone) — same domain (`daikinone`),
  migrated in place.
- [From `apetrycki/daikinskyport`](#from-apetryckidaikinskyport) — different domain, manual
  switch.

Both projects use Daikin's private Skyport API with your account password. This integration uses
the official Daikin One Open API with an **API key** and an **Integrator Token**. Get both before
you start — see [Prerequisites](../README.md#prerequisites).

Before migrating, read [FEATURE_MATRIX.md](FEATURE_MATRIX.md): the official API returns thermostat
data only, so every equipment-telemetry and air-quality entity goes away.

---

## From `zlangbert/ha-daikinone`

Same domain, so **your existing config entry is upgraded in place** — do not delete it.

### What happens, step by step

1. **Replace the files.** Remove the old `custom_components/daikinone` (or remove the old HACS
   repository) and install this one, then restart Home Assistant.
2. **The config entry migrates automatically, v1 → v2.** On the first load,
   `async_migrate_entry` sets the entry's unique id to your lowercased email, records the entity
   unique-id schema version the old integration used (`entity_uid_schema_version`, defaulting to
   `0` when the key is absent), and bumps the version. **Your stored password is kept for now** —
   nothing is deleted before the new credentials work.
3. **Home Assistant prompts you to reauthenticate.** Setup deliberately fails with
   "Reauthentication required", because a password cannot be turned into an Integrator Token.
   Open **Settings → Devices & Services** and click **Reconfigure** / **Reauthenticate** on the
   Daikin One entry.
4. **Enter three values:** your account **email** (pre-filled; correct the capitalisation if the
   app shows it differently — the API is case-sensitive), the **API key**, and the **Integrator
   Token**.
5. **On success the password is deleted from storage** and the entry reloads. Only then. If
   reauthentication fails, the entry stays exactly as it was, so you can retry.

The integration never converts a password into a token, never calls the private API, and never
deletes your config entry.

### What is preserved

Unique ids are unchanged, so entity ids, history, dashboards and automations survive:

| Entity | Unique id | Preserved |
| --- | --- | --- |
| Climate | `{thermostat_id}-climate` | yes |
| Indoor temperature | `{id}-indoor_temperature` (schema 1) or `{id}-Indoor Temperature` (schema 0) | yes — whichever schema your entry recorded |
| Indoor humidity | `{id}-indoor_humidity` / `{id}-Indoor Humidity` | yes |
| Outdoor temperature | `{id}-outdoor_temperature` / `{id}-Outdoor Temperature` | yes |
| Outdoor humidity | `{id}-outdoor_humidity` / `{id}-Outdoor Humidity` | yes |
| Fan speed select | `{id}-fan_speed` | yes |
| Thermostat device | `identifiers={("daikinone", thermostat_id)}` | yes — same device, same area, same name |

Because the unique ids match, you should **not** see any `_2` duplicate entities. If you do, it
means the entry did not migrate — check the log for a migration error and open an issue with
diagnostics attached.

### What goes away

| Removed | Why | What to do |
| --- | --- | --- |
| Equipment devices (air handler / furnace / heat pump / coil) and every sensor on them | The official API has no equipment data at all | Nothing — they are removed from the device registry automatically at the first successful poll. A device that HA cannot remove on its own can be deleted from its device page. |
| `sensor.<thermostat>_online` (enum sensor) | Replaced by a connectivity **binary sensor** on the thermostat device | Update automations to the new binary sensor (`on` = online). The old sensor is not recreated. |
| Anything in the "Not exposed by official API" rows of [FEATURE_MATRIX.md](FEATURE_MATRIX.md) | Not in the documented API | Delete or rewrite the automations that used them. |

### After migrating: check your automations

```bash
# From your Home Assistant config directory:
grep -rn "daikinone" automations.yaml scripts/ packages/ 2>/dev/null
```

Look for references to equipment sensors (demand, CFM, RPS, power, coil temperatures), the old
`_online` sensor, or any hold/away service call. Those need rewriting; the climate, temperature,
humidity and fan-speed references do not.

Also note two behaviour changes:

- Setting a temperature or mode now turns the thermostat's schedule **off** (Daikin's documented
  `/msp` side effect). The schedule switch reflects that.
- Polling is at least **180 seconds** — Daikin's documented minimum for the official API. If your
  old configuration polled faster, expect coarser history.

---

## From `apetrycki/daikinskyport`

Different domain (`daikinskyport` → `daikinone`), so there is no automatic path: the two
integrations cannot share entity registry entries, and this integration will not touch another
domain's registry.

### Steps

1. Write down the entity ids you use in automations and dashboards (see the table below).
2. **Settings → Devices & Services → Daikin Skyport → Delete.** This frees the old entity ids so
   the new entities can claim the friendly names.
3. Remove the old integration (HACS repository or `custom_components/daikinskyport/`) and restart.
4. Install this integration and add it: **Add Integration → Daikin One**, then enter email, API
   key and Integrator Token.
5. Rename the new entities to the old entity ids where you want automations to keep working
   unchanged (entity settings → **Entity ID**).

### Typical entity id mapping

Old ids depend on your thermostat's name; `hallway` is used as the example.

| `daikinskyport` | This integration | Note |
| --- | --- | --- |
| `climate.hallway` | `climate.hallway` | Same platform; modes and setpoints map directly |
| `sensor.hallway_indoor_temperature` | `sensor.hallway_indoor_temperature` | |
| `sensor.hallway_indoor_humidity` | `sensor.hallway_indoor_humidity` | |
| `sensor.hallway_outdoor_temperature` | `sensor.hallway_outdoor_temperature` | |
| `sensor.hallway_outdoor_humidity` | `sensor.hallway_outdoor_humidity` | |
| `sensor.hallway_fan_state` | `sensor.hallway_system_fan` | Enum `auto` / `on`, diagnostic |
| `binary_sensor.hallway_online` | `binary_sensor.hallway_connectivity` | Device class connectivity |
| `switch.hallway_schedule` | `switch.hallway_schedule` | |
| `select.hallway_fan_circulate` | `select.hallway_fan_circulation` | Config category |
| `select.hallway_fan_speed` | `select.hallway_fan_circulation_speed` | Config category |
| `sensor.hallway_aqi*`, `*_voc`, `*_ozone`, `*_particles` | — | Not exposed by official API |
| `sensor.hallway_*_demand`, `*_cfm`, `*_power`, `*_rps`, `*_coil_temp`, `*_runtime` | — | Not exposed by official API |
| `climate` attributes for humidity setpoint, away, holds, one clean, night mode | — | Not exposed by official API |

Anything with `—` in the right column has no replacement — the data does not exist in Daikin's
official API. Automations referencing those entities must be deleted or rewritten; Home Assistant
will otherwise log "Entity not found" every time they run.

### Feature loss

The full comparison, feature by feature, is in [FEATURE_MATRIX.md](FEATURE_MATRIX.md). The short
version: you keep climate control, the four temperature/humidity sensors, fan circulation,
schedule enable/disable and online status; you lose all equipment telemetry, air quality, schedule
editing, holds, away control, target humidity, One Clean, night mode, efficiency priority, fault
codes and the weather forecast.

---

## Rolling back

This integration only ever writes to its own config entry. If you want to go back to a
password-based integration, delete the Daikin One entry, reinstall the old integration and set it
up again — nothing here prevents that. Revoke the Integrator Token in the SkyportHome app when you
stop using it.
