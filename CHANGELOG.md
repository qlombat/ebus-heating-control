# Changelog

## 1.8.8
- `docs/installation-guides.md`: added a safety/responsibility warning at the top (electrical/gas risk, potential warranty/regulatory impact of removing a factory room controller, loss of the manufacturer's safety/comfort logic, no-warranty disclaimer) before the step-by-step instructions.

## 1.8.7
- Moved the real-installation walkthrough out of the README into a dedicated, growable [`docs/installation-guides.md`](docs/installation-guides.md), rewritten in an actionable step-by-step "how to" style (numbered steps, imperative instructions, troubleshooting pointers) instead of a narrative retrospective — meant to make room for future guides covering other boilers/adapters without bloating the main README.
- Fixed a heading that was accidentally dropped from the README during the previous case-study edit (`## Development` had disappeared, leaving its content unlabeled under `## Limitations`).

## 1.8.6
- README: added a "Case study" section walking through a real installation end to end (Bulex/Vaillant `BAI` boiler, wireless Exacontrol E7 removed and replaced by a Wi-Fi eBUS adapter) — physical wiring, ebusd add-on config, boiler detection output, a manual `SetMode` write with a field-by-field breakdown, and how that manual test maps onto the built-in boiler regulation and weekly schedule features. Documentation only, no code changes.

## 1.8.5
- Translated all remaining German comments, docstrings, error messages, and entity display names across the codebase to English (source code, tests, and the `contrib/ebusd-config/` reference files). This was leftover text from the upstream project; behavior is unchanged. `translations/de.json` (the German HA UI locale) is intentionally kept as-is, since it's an actual localization rather than developer notes.
- A few entity friendly names changed as part of this (e.g. "Warmwasser" -> "Domestic hot water", "Warmwasser-Boost" -> "DHW boost", bridge diagnostic sensor names, "<prefix> Zeitprogramm" -> "<prefix> schedule"). Existing `entity_id`s are unaffected; only the displayed name updates.

## 1.8.4 - BREAKING
- **Domain renamed**: `ebus_bridge` → `ebus_heating_control`, matching the repo/display name. This is a breaking change for existing installs:
  - The integration folder moved from `custom_components/ebus_bridge/` to `custom_components/ebus_heating_control/`.
  - The write service is now `ebus_heating_control.write` (was `ebus_bridge.write`) — update any automation/script that calls it.
  - **After updating, remove the old integration entry and re-add it** (Settings → Devices & services → eBUS Heating Control → ⋮ → Delete, then Add integration again). Home Assistant ties config entries and entity unique IDs to the domain, so an in-place update will not pick this up automatically.
  - Entity IDs keep their existing `<platform>.<object_id>` suffix (only the config entry/domain changes), but a fresh setup is still the only supported migration path for a domain rename.

## 1.8.3
- Display name aligned with the new repo name: `eBUS Bridge` → `eBUS Heating Control` (manifest `name`, `hacs.json`, config flow title in `strings.json`/`en.json`/`de.json`, README). The integration's internal `domain` (`ebus_bridge`) and its device/entity IDs are unchanged, so existing installs and automations referencing them are unaffected.

## 1.8.2
- Repo renamed to `qlombat/ebus-heating-control` (was `qlombat/ebus_bridge`); `manifest.json` (`documentation`, `issue_tracker`) and the README's HACS custom-repository install instructions updated accordingly. The integration's internal `domain` (`ebus_bridge`) is unchanged, since that's the identifier Home Assistant already stores for existing installs and renaming it would break them.
- CI: pinned `ruff==0.16.8` in `validate.yml` (was unpinned, so CI silently picked up a much newer ruff than tested locally and started failing on new default lints); fixed the resulting `I001` (import order) and `RUF012` (mutable class-level default) findings in `climate.py`/`services.py`.
- Repo housekeeping: enabled Issues and added GitHub topics (required by HACS validation), added branch protection on `main`.

## 1.8.1
- Boiler regulation now skips writing `SetMode` entirely while the boiler's physical/global winter-mode switch (`HeatingSwitch`) is off, instead of writing every cycle regardless. ebusd's log showed periodic `send to 08: ERR: read timeout, retry` bus errors that correlated with `HeatingSwitch` being off (the boiler appears to respond more sluggishly on the bus in that state); `SetMode` has no effect anyway while this switch is off, so there is no reason to keep sending it.
- Fix: `EbusdBoilerClimate`'s flame-status lookup used the wrong field key (`(circuit, "Flame", "Flame")` instead of `(circuit, "Flame", "value")`, since ebusd's JSON names single-field messages' field "value", not the message name), so `hvac_action` never actually reported HEATING/IDLE from the real flame state.

## 1.8.0
- New: optional weekly heating schedule for the boiler regulation `climate` entity. A new `calendar` entity ("Heating schedule") appears next to it whenever boiler regulation is enabled, editable via HA's standard calendar UI (create/update/delete events; event title = target temperature, e.g. "21 °C"). Each event is a recurring weekly slot (day of week + time window), stored locally (not on the eBUS device). The active slot's temperature is applied automatically; manually changing the target temperature on the climate entity temporarily overrides the schedule until the next slot change, then the schedule resumes. Windows spanning midnight (e.g. 22:00-06:00) are supported.
- New pure `schedule.py` module (unit-tested, no HA dependency) computing which schedule slot is active; persistence lives in `schedule_store.py`.

## 1.7.3
- Boiler regulation defaults recalibrated for classic radiators with thermostatic valves: curve slope 1.2 → 1.7, minimum flow temperature 30 → 40 °C, maximum flow 55 → 65 °C, no-outdoor-sensor base flow 35 → 45 °C. The old defaults were tuned closer to underfloor heating and produced flow temperatures too low for radiators to emit noticeable heat in mild weather (e.g. ~33 °C at 14 °C outside), even though the boiler was correctly modulating. New defaults are calibrated against a real installation that previously ran ~60 °C flow at 0 °C outside / 22 °C room target. Existing installations keep their already-saved option values; this only changes what new installs see as pre-filled defaults.

## 1.7.2
- Boiler regulation now works without an outdoor sensor: it's now optional (only the room sensor is required to enable the feature). Without it, the heating curve is skipped and a configurable base flow temperature (`boiler_base_flow`, default 35 °C) is used instead, with the room PI correction still applying.
- Fix: `SetMode`'s `hcmode` field is now always kept at `auto`, never set to `off` — `hcmode=off` is a coarse whole-boiler mode switch that likely also disables DHW production, defeating the goal of keeping domestic hot water independent from this entity's heating on/off. Heating is now exclusively gated through the granular `disablehc` bit in every state (user Off, hysteresis-satisfied, or actively heating), leaving `HwcSwitch`/`hwctempdesired` completely unaffected.

## 1.7.1
- Fix: boiler regulation never actually stopped calling for heat once the room reached its setpoint — the heating-curve formula alone always returns a flow temperature > 0. Added a heat-demand hysteresis (`should_call_for_heat`, new option `boiler_hysteresis`, default 0.3 °C): once the room exceeds target by this margin, `SetMode`'s `disablehc` bit is set (DHW stays unaffected) until it drops back below target.

## 1.7.0
- New optional **boiler regulation**: for BAI-style boilers with no room controller left on the bus (e.g. after removing an Exacontrol), a `climate` entity now computes a modulating flow-temperature setpoint (heating curve + room PI, see `regulation.py`) from a configurable room and outdoor sensor, and writes it via `SetMode` — no more on/off, real modulation. Opt-in via new Options: room/outdoor sensor, curve slope, Kp/Ki, min/max flow temperature, write interval.
- Fix: `network.async_get_source_ip` call in config flow used the removed `target` kwarg; renamed to `target_ip` (current HA API).
- Fix: circuit devices now link to the bridge device via `via_device_id` (resolved registry id) instead of the deprecated `via_device` identifier tuple.
- Fix: never-polled messages with a `lastup: 0` entry (as opposed to no entry at all) were wrongly treated as already read and never got their initial forced read.
- Fix: passively-monitored multi-field commands (e.g. `SetMode`) are now excluded from forced reads; ebusd cannot answer an active read for them, which was logging `ERR: end of input reached` every cycle.

## 1.6.12
- Catch-up reads now run a few in parallel and use more of each cycle, so entities fill several times faster after a reload.

## 1.6.11
- Fault-memory sensors (`Currenterror`) now show "ok" when there is no fault instead of appearing unavailable.

## 1.6.10
- Rarely-changing values are refreshed every 30 min instead of every 10, so the top-up backlog no longer stays permanently full and entities fill faster after a reload.

## 1.6.9
- Fix: never-polled registers kept filling only until other values aged, then stalled; they now get a small guaranteed quota each cycle and fill completely.

## 1.6.8
- Never-polled registers fill about twice as fast after a restart (still idle-only, so normal traffic stays responsive).

## 1.6.7
- The initial read of never-polled registers is now gentle (only when nothing stale is due, a couple per cycle), so it no longer slows down the bus.

## 1.6.6
- Read-only registers that no bus master polls now get an initial forced read, so their entities become available instead of staying "unavailable".

## 1.6.5
- Every scanned eBUS device now appears with its firmware/hardware even when it exposes no readable values (e.g. the sensoNET gateway).

## 1.6.4
- Auto-removed messages are retried hourly and revived on their own once they decode again, so a corrected CSV definition no longer needs an integration reload.
- Zone name and short-name entities now use a rename icon.

## 1.6.3
- Writable text/date fields (e.g. zone short names) now show up as read-only sensors instead of getting no entity at all.

## 1.6.2
- Messages whose ebusd response keeps failing to decode are dropped from the read rotation, so a wrong CSV definition no longer wastes a read and logs an error every cycle.

## 1.6.1
- Display precision now follows the value divisor, so scaled integers (COP, current power, daily yields) show one decimal instead of being rounded to whole numbers.

## 1.6.0
- New ebusd messages are picked up automatically (periodic definition sync), so an integration reload is no longer needed after editing ebusd config.
- Passively observed command fields (e.g. the regulator's SetMode releasebackup) are exposed as read-only diagnostic sensors, disabled by default.

## 1.5.4
- Rarely-changing config/counter values are topped up only every ~10 min instead of every 90 s, so the top-up converges instead of churning the bus.

## 1.5.3
- Fix: stale values were never topped up because message timestamps need `full`, not `verbose`.

## 1.5.2
- Stale messages are topped up at 20 per cycle while catching up, easing back to 8 once the backlog is gone.

## 1.5.1
- Entities now appear as soon as a value arrives, instead of only those that happened to be cached at startup.

## 1.5.0
- Values the bus refreshes on its own are left untouched; only genuinely stale messages are topped up, a few per cycle.
- Removed the `poll_priority` option: registering hundreds of ebusd polls slowed every value down instead of speeding it up.

## 1.4.0
- New option `fast`: selected messages are read straight from the bus each cycle, so they stay as fresh as the poll interval.

## 1.3.0
- Integration registers ebusd poll priorities itself, so values stay fresh without MQTT (option `poll_priority`, 0 = off).

## 1.2.1
- Sensor display precision derived from the data type (integer registers show without decimals).

## 1.2.0
- New `climate` entity per active heating zone (`Z<n>RoomTemp` / `Z<n>DayTemp` / `Z<n>OpMode`).

## 1.1.0
- New `water_heater` for DHW (`HwcStorageTemp` / `HwcTempDesired` / `HwcOpMode`).
- New `switch` "Warmwasser-Boost" (`HwcSFMode` = load/auto).

## 1.0.0
- Config flow pre-fills the host with the HA IP (editable for remote ebusd).
- First public release (HACS).

## 0.10.0
- Bundled brand icon (`brand/` folder); local icon takes priority over the brands CDN (HA 2026.3+).

## 0.9.0
- Fix: °C setpoints no longer clamped to 0–100 (range −60…150, negative values possible).
- Bridge device is named just "eBUS Bridge"; host exposed as its own text sensor (diagnostic).
- Unit tests for `model.py` + CI (hassfest, HACS, ruff, pytest); `codeowners` set.

## 0.8.0
- Bridge diagnostics from ebusd's global section (signal, symbol rate, reconnects, masters, QQ, version).
- Enhanced timing (arbitration/latency) as diagnostics, disabled by default.

## 0.7.3
- Icon heuristic, unit-first: temperatures always as a thermometer variant.

## 0.7.2
- Meaningful mdi icons for all entities instead of generic slider symbols.

## 0.7.1
- Clean per-circuit device names (no "ebusd" prefix); bridge parent device, circuits as children (`via_device`).

## 0.7.0
- Writable calendar (create/update/delete) once ebusd exposes a timer write message.

## 0.6.0
- New `ebus_bridge.write` service (generic pass-through to ebusd's `write`, with read-back).

## 0.5.0
- Renamed to "eBUS Bridge" (domain `ebus_bridge`); new switch platform; `issue_tracker` added.

## 0.4.1
- Fix: forced read after each write so setpoints don't fall to "unavailable".

## 0.4.0
- New binary_sensor + calendar (read-only) platforms; options flow (poll interval, exclude).

## 0.3.0
- Switched to ebusd HTTP-JSON (read) + TCP (write); field-based entity model; device metadata.
