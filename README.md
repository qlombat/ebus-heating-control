# eBUS Heating Control (Home Assistant)

**Local eBUS heating integration for Home Assistant — no cloud, no MQTT.**

A native Home Assistant custom integration that talks directly to [**ebusd**](https://github.com/john30/ebusd)
and automatically builds devices and entities from whatever your eBUS bus exposes —
no manual entity mapping, no MQTT broker in between. Works with any eBUS vendor
ebusd supports (Vaillant, Wolf, Kromschröder/Elster, Brötje, Ferroli, …), and adds
an optional weather-compensated modulating heating regulation on top for boilers
that no longer have a room controller on the bus.

> ### 🔀 This is a fork
> This repository is a fork of [**dneprojects/ebus_bridge**](https://github.com/dneprojects/ebus_bridge),
> which remains the original author and copyright holder (see [LICENSE](LICENSE)).
> All credit for the base integration — the ebusd HTTP/TCP client, the coordinator,
> and the sensor/number/select/switch/water_heater/climate/calendar platforms — goes
> to that project.
>
> This fork adds a **modulating boiler regulation** on top (see
> [Boiler regulation](#boiler-regulation) below): a `climate` entity that computes
> and writes a continuously modulated flow-temperature setpoint from a room sensor,
> an optional outdoor sensor, and a configurable heating curve — instead of a plain
> on/off — plus a companion weekly heating schedule, and a handful of Home Assistant
> compatibility fixes. See [CHANGELOG.md](CHANGELOG.md) for the full list of changes
> since the fork point. If you don't need boiler regulation, the
> [upstream project](https://github.com/dneprojects/ebus_bridge) covers the same base
> feature set.

## Contents
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Entities](#entities)
- [Boiler regulation](#boiler-regulation)
- [Weekly heating schedule](#weekly-heating-schedule)
- [Options](#options)
- [Service: `ebus_heating_control.write`](#service-ebus_heating_controlwrite)
- [Bridge diagnostics](#bridge-diagnostics)
- [Architecture](#architecture)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Guides for real installations](#guides-for-real-installations)
- [Development](#development)
- [License](#license)

## How it works

```
Boiler / heat pump (eBUS)
        │
        ▼
 eBUS adapter (Wi-Fi / serial / USB)
        │
        ▼
 ebusd  ── HTTP-JSON :8889 (read + definitions) ──▶  eBUS Heating Control (this integration)
        └─ TCP command port :8888 (write) ────────▶
                                                          │
                                                          ▼
                                                   Home Assistant entities
```

**ebusd** does the actual eBUS decoding, using its CSV message definitions for your
specific device. This integration is purely the Home Assistant layer on top of it:
- **Read + definitions** come from ebusd's **HTTP-JSON API** (port `8889`) — this is
  what gives exact units, enum option names, and per-field structure (not just raw
  numbers), and lets the integration build entities automatically instead of you
  hand-writing them.
- **Writes** go through ebusd's **TCP command port** (port `8888`).

No MQTT broker is required anywhere in this chain.

## Requirements

- A running **ebusd** with both ports reachable from Home Assistant — easiest via the
  [ebusd Home Assistant add-on](https://github.com/LukasGrebe/ha-addons). ebusd must
  already be decoding your bus (green bus LED / `scan 08` style output showing your
  boiler) before this integration will find anything.
- **Both ports exposed**, with the HTTP port explicitly enabled:
  - `8888/tcp` → `8888` (write)
  - `8889/tcp` → `8889` (HTTP-JSON) — requires **`--httpport=8889`** in the add-on's
    `commandline_options`.
- If you plan to use [boiler regulation](#boiler-regulation) (writing `SetMode`),
  ebusd needs **`--accesslevel=install`** — without it, ebusd rejects the write.
- Quick sanity check in a browser: `http://<HA-IP>:8889/data` should return JSON.

## Installation

**Via HACS** (recommended):
1. HACS → ⋮ → *Custom repositories* → add `https://github.com/qlombat/ebus-heating-control`,
   category *Integration*.
2. Install **eBUS Heating Control** from HACS → restart Home Assistant.

**Manual:** copy `custom_components/ebus_heating_control/` into
`<config>/custom_components/ebus_heating_control/` → restart Home Assistant.

Then: **Settings → Devices & Services → Add integration → "eBUS Heating Control"**. The
**host field is pre-filled with the Home Assistant host IP** (if ebusd runs locally
as an add-on, just confirm; for a remote ebusd, override the IP). Ports `8888`/`8889`
are pre-filled with the defaults.

## Entities

Entities are derived automatically from whatever ebusd's definitions expose — the
entity set is **fixed** by those definitions (no leftover entities from a previous,
different device). After every write (`number`/`select`/`switch`), the integration
performs a fresh read (`read -f`) so setpoints ebusd doesn't poll on its own don't
end up stuck as "unavailable".

| Platform | What it exposes |
|---|---|
| `sensor` | One entity per non-writable field (multi-field messages are split, e.g. `Status01` → separate flow/return/… sensors), with unit and device class taken from ebusd's definitions. |
| `binary_sensor` | Non-writable pure on/off fields (e.g. pump status). |
| `number` | Writable numeric setpoints. Min/max/step are derived from the underlying ebusd data type (°C setpoints: −60…150, step 0.5). |
| `select` | Writable enum fields (operating modes), with their real option names. |
| `switch` | Writable pure on/off fields, plus a curated **"DHW boost"** switch (one-time domestic-hot-water charge) when `HwcSFMode` supports it. |
| `water_heater` | Domestic hot water (DHW) as a proper `water_heater` tile (curated Vaillant overlay: current `HwcStorageTemp`, target `HwcTempDesired`, mode `HwcOpMode`) — only appears when those registers exist. |
| `climate` | One thermostat per active heating zone (curated Vaillant overlay: current `Z<n>RoomTemp`, target = comfort setpoint `Z<n>DayTemp`, `Z<n>OpMode` → HVAC mode/preset off/auto/day/night) — **only appears if those Vaillant zone registers exist on the bus** (typically only with a Vaillant room controller still connected). Plus the optional [boiler regulation](#boiler-regulation) entity, which instead requires `boiler_room_sensor` to be set — **see the warning in that section if no `climate` entity shows up at all.** |
| `calendar` | Vaillant weekly schedules (`<Prefix>Timer_<Day><Slot>`), one calendar per program (Z1/Z2/Z3/Hwc/Cc, …); editable once ebusd exposes a writable per-day message. Plus the optional ["Heating schedule"](#weekly-heating-schedule) calendar for boiler regulation. |

## Boiler regulation

**The headline feature of this fork.** For boilers exposing a `SetMode`-style
composite command (`hcmode` + `flowtempdesired`, as on Vaillant `BAI`-circuit
boilers) with no room controller left on the bus — for example after removing a
physical thermostat such as a Vaillant Exacontrol — this adds a `climate` entity
that replaces it with a proper modulating regulation, instead of a simple on/off
relay.

Every regulation cycle (default 60 s, configurable), it:
1. Reads your configured **room temperature sensor** (required) and, if configured,
   an **outdoor temperature sensor**.
2. Computes a flow-temperature setpoint from a **weather-compensated heating curve**
   plus a **room-temperature PI correction**:
   ```
   flow_base  = room_target + curve_slope × (room_target − outdoor_temp)
   correction = Kp × room_error + integral        (integral += room_error × Ki, clamped)
   flow_setpoint = clamp(flow_base + correction, flow_min, flow_max)
   ```
   Without an outdoor sensor, the heating curve is skipped and a fixed
   **base flow temperature** is used instead — the room PI correction still applies.
3. Applies a **heat-demand hysteresis**: once the room overshoots the target by more
   than the configured margin, it stops calling for heat (until the room drops back
   below target) instead of continuing to modulate a flow setpoint indefinitely, or
   short-cycling right at the setpoint.
4. Writes the result via `SetMode`.

Two behaviours are deliberate design choices, verified against real hardware:

- **`hcmode` is always kept at `"auto"`**, never set to `"off"`. Heating is switched
  on/off exclusively through the granular `disablehc` bit. `hcmode=off` is a coarse,
  whole-boiler mode switch that on at least some models also disables domestic hot
  water production — this entity's on/off state must never affect DHW.
- If the boiler's own physical/global winter-mode switch (`HeatingSwitch`) is off,
  the entity **skips writing `SetMode` entirely** that cycle (rather than writing a
  value that would have no effect anyway) — some boilers respond more sluggishly on
  the bus in that state, and this avoids adding unnecessary bus traffic to it.

`hvac_action` reflects the actual flame state (`heating`/`idle`) whenever
`Flame` is known, and `off` while the entity itself is switched off.

### Enabling it

Boiler regulation is **opt-in and disabled by default** — it only appears once a
**room temperature sensor** is configured in **Settings → Devices & services →
eBUS Heating Control → Configure**:

> ⚠️ **No `climate` entity yet? This is almost always why.** After installing or
> reinstalling the integration, `boiler_room_sensor` is empty by default, so
> `EbusdBoilerClimate` is **not created at all** — no entity, no error, nothing
> in the logs. Set `boiler_room_sensor` below and reload the integration (or
> wait for the next entity-discovery pass) to make it appear. This is
> independent from the curated per-zone thermostats in the [Entities](#entities)
> table above, which instead require Vaillant `Z<n>*` zone registers to exist
> on the bus (typically only present if a Vaillant room controller is still
> connected) — if you removed your room controller (see the
> [installation guides](docs/installation-guides.md)), those won't appear
> either way, and `boiler_room_sensor` is the only path to a `climate` entity.

| Option | Default | Description |
|---|---|---|
| `boiler_room_sensor` | *(none)* | Entity ID of a room temperature sensor. **Required** to enable this feature. |
| `boiler_outdoor_sensor` | *(none)* | Entity ID of an outdoor temperature sensor. Optional — enables the heating curve; without it, `boiler_base_flow` is used as a fixed base instead. |
| `boiler_curve_slope` | `1.7` | Heating-curve slope. Only used with an outdoor sensor. Calibrate from a known reference point, e.g. if your old system ran ~60 °C flow at 0 °C outside with a 22 °C room target: `slope = (60 − 22) / (22 − 0) ≈ 1.7`. Lower for underfloor heating (e.g. `0.3`–`0.6`). |
| `boiler_base_flow` | `45.0` °C | Fixed flow-temperature base used **only** when no outdoor sensor is configured. |
| `boiler_kp` | `3.0` | Proportional gain of the room-temperature correction. |
| `boiler_ki` | `0.1` | Integral gain of the room-temperature correction (anti-windup clamped). |
| `boiler_flow_min` | `40.0` °C | Minimum flow-temperature setpoint. Radiators with thermostatic valves generally need a noticeably higher minimum than underfloor heating to emit any noticeable heat at all. |
| `boiler_flow_max` | `65.0` °C | Maximum flow-temperature setpoint. |
| `boiler_hysteresis` | `0.3` °C | How far the room may overshoot the target before heat demand stops; prevents short-cycling right at the setpoint. |
| `boiler_write_interval` | `60` s | How often `SetMode` is recomputed and (re)written. Also acts as a heartbeat: every cycle rewrites the value regardless of whether it changed. |

The formula and hysteresis logic live in
[`custom_components/ebus_heating_control/regulation.py`](custom_components/ebus_heating_control/regulation.py) —
pure functions with no Home Assistant dependency, unit-tested in
[`tests/test_regulation.py`](tests/test_regulation.py).

## Weekly heating schedule

Whenever [boiler regulation](#boiler-regulation) is enabled, a companion
**"Heating schedule"** `calendar` entity appears automatically. Unlike the
eBUS-timer-backed `EbusdCalendar` (which mirrors on-device Vaillant schedules), this
one is **entirely Home Assistant-side** — not tied to any eBUS message, stored
locally under `.storage/`, and always fully editable through the standard Home
Assistant calendar UI:

- Create, update, or delete events — each is a **recurring weekly time slot**
  (day of week + time window).
- The **event title holds the target temperature**, e.g. `21°C`.
- Windows spanning midnight are supported (e.g. Saturday `22:00`–`06:00` for a
  night setback).
- The boiler regulation `climate` entity automatically applies the active slot's
  temperature every regulation cycle.
- **Manually changing the target temperature** on the climate entity temporarily
  overrides the schedule until the next slot boundary, then the schedule resumes —
  the same behaviour you'd expect from a typical programmable thermostat.

The slot-lookup logic lives in
[`custom_components/ebus_heating_control/schedule.py`](custom_components/ebus_heating_control/schedule.py)
(pure, unit-tested in [`tests/test_schedule.py`](tests/test_schedule.py));
persistence is handled by
[`schedule_store.py`](custom_components/ebus_heating_control/schedule_store.py) via
`homeassistant.helpers.storage.Store`, with a single store instance shared between
the `climate` and `calendar` entities so edits apply on the very next cycle.

## Options

**Settings → Devices & services → eBUS Heating Control → Configure**:

| Option | Default | Description |
|---|---|---|
| Poll interval | `30` s | How often the integration polls ebusd's `/data` endpoint. |
| Exclude | `Timer` | Comma-separated name substrings to hide from `sensor`/`binary_sensor`/etc. (the calendar platform still uses excluded timer messages). |
| Fast | *(none)* | Comma-separated name substrings read directly from the bus every cycle instead of waiting for ebusd's own poll rotation — keeps them as fresh as the poll interval. Use sparingly: each entry costs one forced bus read per cycle. |
| *(Boiler regulation options)* | — | See the [Boiler regulation](#boiler-regulation) table above. |

## Service: `ebus_heating_control.write`

Generic pass-through to ebusd's `write` command, for anything not covered by the
generated entities:

```yaml
action: ebus_heating_control.write
data:
  circuit: bai
  message: SetMode
  value: "auto;45.0;-;-;0;0;0;0;0;0"
```

- `circuit` / `message` — target the eBUS message (as shown in ebusd's `/data`).
- `value` — separate multiple fields with `;` (e.g. `0;3;06:00;22:00;20.0`).
- `entry_id` — optional, only needed with more than one eBUS Heating Control config entry.

Reads the value back after writing and returns it as the service response. Lets you
set **any** writable ebusd message, including ones with no dedicated entity yet.

## Bridge diagnostics

The bridge device (category *diagnostic*) exposes ebusd's global bus health from its
global section: **signal** (connectivity), **symbol rate** / max, **reconnects**,
**masters on the bus**, **QQ**, **known messages**; ebusd's own **version** is shown
as the device's `sw_version`. The enhanced-timing values (arbitration/latency) are
diagnostic entities, **disabled by default**.

## Architecture

| File | Responsibility |
|---|---|
| `model.py` | Parses ebusd's JSON into field descriptors (`parse_definitions` / `parse_values` / `parse_device_meta`). |
| `client.py` | HTTP-JSON client (read/definitions) + TCP client (write, and a forced `read -f` after writing). |
| `coordinator.py` | Fetches definitions once, values on a cycle; applies the exclude filter; top-up logic for values ebusd doesn't refresh on its own. |
| `entity.py` | Shared base entity + device-info helper for all platforms. |
| `sensor.py` / `binary_sensor.py` / `number.py` / `select.py` / `switch.py` / `water_heater.py` / `calendar.py` | The generic, definition-driven platforms. |
| `climate.py` | Curated per-zone thermostats, plus `EbusdBoilerClimate` (see [Boiler regulation](#boiler-regulation)). |
| `regulation.py` | Pure, unit-tested heating-curve + room-PI formula used by `EbusdBoilerClimate`. No Home Assistant import. |
| `schedule.py` | Pure, unit-tested weekly-schedule slot lookup used by the "Heating schedule" calendar. No Home Assistant import. |
| `schedule_store.py` | Persists the weekly schedule via `homeassistant.helpers.storage.Store`, shared between `climate.py` and `calendar.py`. |
| `config_flow.py` | Config flow (host + both ports, tests connectivity) and options flow (poll interval, exclude, boiler regulation settings). |
| `services.py` | The `ebus_heating_control.write` service. |

## Limitations

- `number` bounds are derived from the ebusd data type — ebusd's JSON doesn't expose
  explicit min/max, so box-mode input is intentionally tolerant.
- Values update on the configured poll interval (default 30 s) — one `/data`
  round-trip per cycle, plus a small, bandwidth-limited "top-up" allowance for
  values the bus doesn't refresh on its own.
- Boiler regulation writes `SetMode` unconditionally every cycle while active (as a
  heartbeat) — if Home Assistant or ebusd goes down, the boiler keeps whatever
  setpoint it last received until reached its own internal failsafe/timeout
  behaviour, if any. There is currently no separate watchdog on the Home Assistant
  side beyond the regular write cycle.

## Troubleshooting

- **No `climate` entity appears at all.** See the warning box in
  [Enabling it](#enabling-it) above — the boiler regulation `climate` entity
  requires `boiler_room_sensor` to be set in the integration's options, and is
  silently skipped (no entity, no error) if it isn't. The curated per-zone
  thermostats are separate and require Vaillant `Z<n>*` zone registers to exist
  on the bus in the first place (normally only present with a Vaillant room
  controller still connected) — removing that controller, as described in the
  [installation guides](docs/installation-guides.md), means those will never
  appear, regardless of options.
- **Entities stuck on "unavailable" after upgrading from a version before
  1.8.4, or after renaming/reinstalling the integration.** Home Assistant ties
  config entries and entity unique IDs to the integration's `domain`. If you
  upgraded across the `ebus_bridge` → `ebus_heating_control` domain rename (see
  [CHANGELOG.md](CHANGELOG.md)) without removing and re-adding the integration,
  you'll have two sets of entities: the old ones (stuck "unavailable" forever,
  since nothing updates them anymore) and the new, working ones. Delete the
  integration's old config entry (or the individual orphaned entities under
  **Settings → Devices & services → Entities**, filtered by state
  "Unavailable") — this is just leftover bookkeeping and safe to remove.
- **A newly created `sensor`/`number`/etc. entity is "unavailable" right after
  setup.** This is expected initially: the integration only creates an entity
  once ebusd has actually returned a value for it at least once (see
  [`add_fields_dynamically`](custom_components/ebus_heating_control/entity.py)),
  and fills in the rest of the backlog gradually afterwards (see
  [Architecture](#architecture) → `coordinator.py`). Give it a few polling
  cycles; a full backlog catch-up after a fresh ebusd restart can take longer
  (see the top-up logic in `coordinator.py`) but should resolve on its own.
- **You changed something directly at the boiler (a physical switch, its own
  control panel, a manual eBUS write outside Home Assistant, …) and Home
  Assistant doesn't reflect it yet.** This is normal, not a bug — most fields
  only update on the configured poll interval (default 30 s per cycle), and
  values ebusd itself doesn't actively poll rely on this integration's
  "top-up" catch-up logic (see [Limitations](#limitations) and
  [Architecture](#architecture) → `coordinator.py`), which is intentionally
  conservative to avoid flooding the bus. **It can take several minutes** for
  a manual change to show up as an updated entity state in Home Assistant,
  especially right after ebusd itself was restarted. Give it a few minutes
  before assuming something is broken.

## Guides for real installations

Step-by-step walkthroughs for specific real hardware setups (physical wiring,
ebusd add-on config, a manual `SetMode` test, etc.) live in
[`docs/installation-guides.md`](docs/installation-guides.md) rather than here,
so more can be added over time without cluttering this reference doc.

## Development

```bash
pip install ruff pytest
ruff check custom_components/
pytest -q
```

CI (`.github/workflows/validate.yml`) runs `hassfest`, a HACS validation, `ruff`,
and the test suite on every push/PR. `regulation.py` and `schedule.py` are pure
Python with no Home Assistant dependency, so they're tested directly with `pytest`
in `tests/`, the same pattern used for `model.py`.

## License

MIT — see [LICENSE](LICENSE). Original copyright
[dneprojects](https://github.com/dneprojects) ([upstream project](https://github.com/dneprojects/ebus_bridge)).
