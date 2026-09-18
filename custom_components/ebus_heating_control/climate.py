"""Climate platform: heating circuits (zones) as HA climate entities.

Curated Vaillant overlay: detects the zone registers
`Z<n>DayTemp` / `Z<n>RoomTemp` / `Z<n>OpMode` and builds a thermostat for each
**active** zone: target = comfort setpoint (`Z<n>DayTemp`), current =
`Z<n>RoomTemp`, modes from `Z<n>OpMode` (off->OFF, auto->AUTO, otherwise
HEAT + preset). If the registers are missing (non-Vaillant), no device is
created. The raw entities remain available in addition.

Additionally (optional, see `EbusdBoilerClimate`): a modulating boiler
regulation for BAI circuits with no room controller of their own (e.g. after
removing an Exacontrol) -- doesn't replace a zone register, but periodically
writes a self-computed flow setpoint via `SetMode`. Optionally driven by a
weekly schedule (see `calendar.EbusdHeatingScheduleCalendar` + `schedule.py`)
instead of a single fixed setpoint.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, ClassVar

import homeassistant.util.dt as dt_util
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import EbusdError
from .const import (
    CONF_BOILER_BASE_FLOW,
    CONF_BOILER_CURVE_SLOPE,
    CONF_BOILER_FLOW_MAX,
    CONF_BOILER_FLOW_MIN,
    CONF_BOILER_HYSTERESIS,
    CONF_BOILER_KI,
    CONF_BOILER_KP,
    CONF_BOILER_OUTDOOR_SENSOR,
    CONF_BOILER_ROOM_SENSOR,
    CONF_BOILER_WRITE_INTERVAL,
    DEFAULT_BOILER_BASE_FLOW,
    DEFAULT_BOILER_CURVE_SLOPE,
    DEFAULT_BOILER_FLOW_MAX,
    DEFAULT_BOILER_FLOW_MIN,
    DEFAULT_BOILER_HYSTERESIS,
    DEFAULT_BOILER_KI,
    DEFAULT_BOILER_KP,
    DEFAULT_BOILER_WRITE_INTERVAL,
    DOMAIN,
)
from .coordinator import EbusdCoordinator
from .entity import build_device_info
from .model import FieldDesc
from .regulation import (
    RegulationParams,
    compute_flow_setpoint,
    format_setmode,
    should_call_for_heat,
)
from .schedule import active_setpoint
from .schedule_store import HeatingScheduleStore

_LOGGER = logging.getLogger(__name__)

_ROLE = {"DayTemp": "day", "RoomTemp": "room", "OpMode": "op"}
_ZONE_RE = re.compile(r"^Z(\d+)(DayTemp|RoomTemp|OpMode)$")

# Fields that enable a boiler SetMode regulation (see regulation.py).
_SETMODE_FIELDS = {"hcmode", "flowtempdesired"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EbusdCoordinator = hass.data[DOMAIN][entry.entry_id]
    zones: dict[tuple[str, int], dict[str, FieldDesc]] = {}
    for d in coordinator.fields:
        m = _ZONE_RE.match(d.message)
        if m:
            zones.setdefault((d.circuit, int(m.group(1))), {})[_ROLE[m.group(2)]] = d

    entities: list[Any] = []
    for (circuit, n), parts in sorted(zones.items()):
        day = parts.get("day")
        if day is None or not day.writable:
            continue  # no thermostat without a writable comfort setpoint
        # only create active zones (with actual data)
        if not any(coordinator.data.get(p.key) is not None for p in parts.values()):
            continue
        entities.append(
            EbusdZoneClimate(
                coordinator, circuit, n, day, parts.get("room"), parts.get("op")
            )
        )
    entities.extend(_build_boiler_climates(coordinator, entry))
    async_add_entities(entities)


def _build_boiler_climates(
    coordinator: EbusdCoordinator, entry: ConfigEntry
) -> list[EbusdBoilerClimate]:
    """Boiler regulation: only created when a room sensor is configured.

    Opt-in via the options flow -- without a room sensor there's no
    meaningful setpoint to compute, so it's better to have no entity at all
    than a broken one. The outdoor sensor is optional (see
    `regulation.compute_flow_setpoint`): without it only the heating curve
    is skipped, the room-PI regulation still runs.
    """
    room_sensor = entry.options.get(CONF_BOILER_ROOM_SENSOR)
    outdoor_sensor = entry.options.get(CONF_BOILER_OUTDOOR_SENSOR) or None
    if not room_sensor:
        return []

    circuits = boiler_circuits(coordinator)

    params = RegulationParams(
        curve_slope=float(
            entry.options.get(CONF_BOILER_CURVE_SLOPE, DEFAULT_BOILER_CURVE_SLOPE)
        ),
        kp=float(entry.options.get(CONF_BOILER_KP, DEFAULT_BOILER_KP)),
        ki=float(entry.options.get(CONF_BOILER_KI, DEFAULT_BOILER_KI)),
        flow_min=float(
            entry.options.get(CONF_BOILER_FLOW_MIN, DEFAULT_BOILER_FLOW_MIN)
        ),
        flow_max=float(
            entry.options.get(CONF_BOILER_FLOW_MAX, DEFAULT_BOILER_FLOW_MAX)
        ),
        hysteresis=float(
            entry.options.get(CONF_BOILER_HYSTERESIS, DEFAULT_BOILER_HYSTERESIS)
        ),
        base_flow=float(
            entry.options.get(CONF_BOILER_BASE_FLOW, DEFAULT_BOILER_BASE_FLOW)
        ),
    )
    interval = int(
        entry.options.get(CONF_BOILER_WRITE_INTERVAL, DEFAULT_BOILER_WRITE_INTERVAL)
    )
    return [
        EbusdBoilerClimate(
            coordinator, circuit, room_sensor, outdoor_sensor, params, interval,
            coordinator.heating_schedule_stores.get(circuit),
        )
        for circuit in circuits
    ]


def boiler_circuits(coordinator: EbusdCoordinator) -> list[str]:
    """eBUS circuits that support a boiler modulation regulation via `SetMode`.

    Public, so `__init__.py` (creating the schedule store before platform
    setup) and `calendar.py` (schedule calendar per circuit) can reuse the
    same detection instead of duplicating it.
    """
    circuits: dict[str, set[str]] = {}
    for d in coordinator.fields:
        if d.message == "SetMode" and d.field in _SETMODE_FIELDS:
            circuits.setdefault(d.circuit, set()).add(d.field)
    return sorted(c for c, fields in circuits.items() if _SETMODE_FIELDS <= fields)



class EbusdZoneClimate(CoordinatorEntity[EbusdCoordinator], ClimateEntity):
    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 5
    _attr_max_temp = 30
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        coordinator: EbusdCoordinator,
        circuit: str,
        zone: int,
        day: FieldDesc,
        room: FieldDesc | None,
        opmode: FieldDesc | None,
    ) -> None:
        super().__init__(coordinator)
        self._day = day
        self._room = room
        self._op = opmode if (opmode and opmode.writable and opmode.values) else None
        self._attr_name = f"Heating circuit {zone}"
        self._attr_unique_id = f"{DOMAIN}_{circuit}_z{zone}_climate".lower()
        self._attr_device_info = build_device_info(coordinator, circuit)

        names = list((self._op.values or {}).values()) if self._op else []
        low = {v.lower(): v for v in names}
        self._off = low.get("off")
        self._auto = low.get("auto")
        self._presets = [v for v in names if v.lower() not in ("off", "auto")]
        self._heat_default = low.get("day") or (self._presets[0] if self._presets else None)

        modes = [HVACMode.HEAT]
        if self._off:
            modes.insert(0, HVACMode.OFF)
        if self._auto:
            modes.append(HVACMode.AUTO)
        self._attr_hvac_modes = modes

        feature = ClimateEntityFeature.TARGET_TEMPERATURE
        if self._off:
            feature |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._presets:
            self._attr_preset_modes = self._presets
            feature |= ClimateEntityFeature.PRESET_MODE
        self._attr_supported_features = feature

    def _val(self, desc: FieldDesc | None) -> Any:
        return self.coordinator.data.get(desc.key) if desc else None

    @property
    def current_temperature(self) -> float | None:
        try:
            return float(self._val(self._room))
        except (TypeError, ValueError):
            return None

    @property
    def target_temperature(self) -> float | None:
        try:
            return float(self._val(self._day))
        except (TypeError, ValueError):
            return None

    @property
    def hvac_mode(self) -> HVACMode:
        value = self._val(self._op)
        low = str(value).lower() if value is not None else ""
        if low == "off":
            return HVACMode.OFF
        if low == "auto":
            return HVACMode.AUTO
        return HVACMode.HEAT

    @property
    def preset_mode(self) -> str | None:
        value = self._val(self._op)
        return value if value in (self._presets or []) else None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        out: object = int(temp) if float(temp).is_integer() else temp
        await self._write(self._day, out)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if not self._op:
            return
        if hvac_mode == HVACMode.OFF and self._off:
            await self._write(self._op, self._off)
        elif hvac_mode == HVACMode.AUTO and self._auto:
            await self._write(self._op, self._auto)
        elif hvac_mode == HVACMode.HEAT and self._heat_default:
            await self._write(self._op, self._heat_default)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if self._op and preset_mode in (self._presets or []):
            await self._write(self._op, preset_mode)

    async def async_turn_off(self) -> None:
        if self._op and self._off:
            await self._write(self._op, self._off)

    async def async_turn_on(self) -> None:
        if self._op and self._heat_default:
            await self._write(self._op, self._heat_default)

    async def _write(self, desc: FieldDesc, value: object) -> None:
        try:
            await self.coordinator.client.write(desc.circuit, desc.message, value)
        except EbusdError as err:
            raise HomeAssistantError(f"Zone write failed: {err}") from err
        try:
            await self.coordinator.client.read(desc.circuit, desc.message)
        except EbusdError:
            pass
        await self.coordinator.async_request_refresh()


class EbusdBoilerClimate(CoordinatorEntity[EbusdCoordinator], RestoreEntity, ClimateEntity):
    """Modulating boiler regulation with no room controller of its own on the bus.

    Replaces an outdoor-temperature-compensated room thermostat (e.g. a
    removed Exacontrol): `target_temperature` is the room setpoint,
    `current_temperature` comes from a freely selectable external sensor.
    Instead of just switching on/off, a self-computed flow setpoint (heating
    curve + room PI, see `regulation.py`) is periodically written via the
    eBUS `SetMode` command -- modulating, as the BAI boiler electronics
    themselves expect. The outdoor sensor is optional: without it only the
    heating curve is skipped (pure room-PI regulation around `params.base_flow`).

    Affects only the heating function (`disablehc` bit) -- domestic hot water
    production (`HwcSwitch`, `hwctempdesired`) stays completely untouched in
    every state of this entity (including HVACMode.OFF), see
    `regulation.format_setmode`.

    Optional weekly schedule (see `calendar.EbusdHeatingScheduleCalendar` and
    `schedule.py`): automatically adjusts `target_temperature` to the
    currently active slot. A manual `async_set_temperature` overrides the
    schedule until the next slot change (see `_apply_schedule`).

    Every cycle writes anew, regardless of whether anything changed -- this
    also serves as a heartbeat for the boiler (failsafe behavior on HA/ebusd
    failure then falls back to the boiler's own electronics). Exception: if
    the physical/global winter-mode switch (`HeatingSwitch`) is off, nothing
    is written at all -- the boiler then tends to respond more sluggishly on
    the bus (more "read timeout" entries in ebusd's log), and SetMode has no
    effect anyway while this switch is in that state.
    """

    _attr_has_entity_name = True
    _attr_name = "Heating"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 10
    _attr_max_temp = 28
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: EbusdCoordinator,
        circuit: str,
        room_sensor: str,
        outdoor_sensor: str | None,
        params: RegulationParams,
        write_interval: int,
        schedule_store: HeatingScheduleStore | None,
    ) -> None:
        super().__init__(coordinator)
        self._circuit = circuit
        self._room_sensor = room_sensor
        self._outdoor_sensor = outdoor_sensor
        self._params = params
        self._write_interval = write_interval
        self._schedule_store = schedule_store
        # The field name in ebusd's JSON for generic single-field messages is
        # "value" (not the message name) -- verified on the real device.
        self._flame_key = (circuit, "Flame", "value")
        self._heating_switch_key = (circuit, "HeatingSwitch", "value")
        self._integral = 0.0
        self._calling_for_heat = False
        # Weekly schedule: as soon as the user manually sets a temperature,
        # that counts as an override against the schedule -- until the
        # schedule value changes at the next slot change (see `_async_regulate`).
        self._schedule_override = False
        self._schedule_baseline: float | None = None
        self._attr_unique_id = f"{DOMAIN}_{circuit}_boiler_climate".lower()
        self._attr_device_info = build_device_info(coordinator, circuit)
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = 20.0
        self._attr_current_temperature = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_restore_state()
        self._update_current_temperature()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._room_sensor], self._handle_room_sensor_change
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_regulate, timedelta(seconds=self._write_interval)
            )
        )
        await self._async_regulate()  # first cycle right away, not after the interval

    async def _async_restore_state(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        if last_state.state in (HVACMode.OFF.value, HVACMode.HEAT.value):
            self._attr_hvac_mode = HVACMode(last_state.state)
        temp = last_state.attributes.get(ATTR_TEMPERATURE)
        if temp is not None:
            try:
                self._attr_target_temperature = float(temp)
            except (TypeError, ValueError):
                pass
        integral = last_state.attributes.get("regulation_integral")
        if integral is not None:
            try:
                self._integral = float(integral)
            except (TypeError, ValueError):
                pass
        calling = last_state.attributes.get("regulation_calling_for_heat")
        if calling is not None:
            self._calling_for_heat = bool(calling)
        override = last_state.attributes.get("schedule_override")
        if override is not None:
            self._schedule_override = bool(override)
        baseline = last_state.attributes.get("schedule_baseline")
        if baseline is not None:
            try:
                self._schedule_baseline = float(baseline)
            except (TypeError, ValueError):
                pass

    @callback
    def _handle_room_sensor_change(self, event: Any) -> None:
        self._update_current_temperature()
        self.async_write_ha_state()

    def _update_current_temperature(self) -> None:
        self._attr_current_temperature = self._read_temperature(self._room_sensor)

    def _read_temperature(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "regulation_integral": round(self._integral, 3),
            "regulation_calling_for_heat": self._calling_for_heat,
            "schedule_override": self._schedule_override,
            "schedule_baseline": self._schedule_baseline,
        }

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if not self._calling_for_heat:
            return HVACAction.IDLE
        flame = self.coordinator.data.get(self._flame_key)
        if flame is None:
            return None
        return HVACAction.HEATING if str(flame).lower() == "on" else HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._attr_target_temperature = float(temp)
        if self._schedule_store is not None:
            # A manual set overrides the schedule until its value changes
            # at the next slot change (see `_apply_schedule`).
            self._schedule_baseline = await self._current_schedule_value()
            self._schedule_override = True
        self.async_write_ha_state()
        await self._async_regulate()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self._attr_hvac_modes:
            return
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()
        await self._async_regulate()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def _current_schedule_value(self) -> float | None:
        """Target temperature of the currently active schedule slot, if any."""
        if self._schedule_store is None:
            return None
        events = await self._schedule_store.async_events()
        if not events:
            return None
        now = dt_util.now()
        return active_setpoint(events, weekday=now.weekday(), now=now.time())

    async def _apply_schedule(self) -> None:
        """Apply the schedule setpoint, unless a manual override is active.

        A manual override (see `async_set_temperature`) persists until the
        schedule value changes from the baseline stored when it was set
        (next slot change) -- after that the schedule takes over again.
        Without a schedule or a matching slot, the last known setpoint stays
        unchanged (e.g. the manually set one, or the one restored on restart).
        """
        schedule_value = await self._current_schedule_value()
        if schedule_value is None:
            return
        if self._schedule_override:
            if schedule_value == self._schedule_baseline:
                return  # still in the same slot as when the override was set
            self._schedule_override = False  # new slot -> override ended
        if self._attr_target_temperature != schedule_value:
            self._attr_target_temperature = schedule_value
        self._schedule_baseline = schedule_value

    async def _async_regulate(self, _now: Any = None) -> None:
        await self._apply_schedule()
        heating_switch = self.coordinator.data.get(self._heating_switch_key)
        if heating_switch is not None and str(heating_switch).lower() == "off":
            # The physical/global winter-mode switch is off: the boiler then
            # tends to respond more sluggishly on the bus (more "read
            # timeout" entries in ebusd's log). SetMode would have no effect
            # anyway while this switch is in that state -- so don't write at
            # all instead of also bothering it with requests. `None` (field
            # not yet read/message not present on this device) does NOT
            # block, so regulation continues normally without this register.
            self._calling_for_heat = False
            self.async_write_ha_state()
            _LOGGER.debug(
                "%s: HeatingSwitch is off, skipping SetMode write",
                self.entity_id,
            )
            return
        active = self.hvac_mode == HVACMode.HEAT
        flow_setpoint: float | None = None
        if active:
            self._update_current_temperature()
            if self._attr_current_temperature is None:
                _LOGGER.warning(
                    "%s: room sensor unavailable, skipping regulation cycle",
                    self.entity_id,
                )
                return
            outdoor: float | None = None
            if self._outdoor_sensor:
                outdoor = self._read_temperature(self._outdoor_sensor)
                if outdoor is None:
                    _LOGGER.warning(
                        "%s: outdoor sensor configured, but unavailable -- "
                        "skipping regulation cycle", self.entity_id,
                    )
                    return
            # outdoor stays None if no outdoor sensor is configured at all --
            # compute_flow_setpoint() then uses params.base_flow instead of
            # the heating curve (see regulation.py).
            self._calling_for_heat = should_call_for_heat(
                target_room=self._attr_target_temperature,
                current_room=self._attr_current_temperature,
                currently_calling=self._calling_for_heat,
                hysteresis=self._params.hysteresis,
            )
            result = compute_flow_setpoint(
                target_room=self._attr_target_temperature,
                current_room=self._attr_current_temperature,
                outdoor_temp=outdoor,
                integral=self._integral,
                params=self._params,
            )
            self._integral = result.integral
            flow_setpoint = result.flow_setpoint
        else:
            self._calling_for_heat = False
        value = format_setmode(flow_setpoint, self._calling_for_heat)
        try:
            await self.coordinator.client.write(self._circuit, "SetMode", value)
        except EbusdError as err:
            _LOGGER.warning(
                "%s: SetMode write failed: %s", self.entity_id, err
            )
            return
        self.async_write_ha_state()
