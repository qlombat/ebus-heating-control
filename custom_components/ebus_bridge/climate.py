"""Climate-Plattform: Heizkreise (Zonen) als HA-climate-Entities.

Kuratierter Vaillant-Overlay: erkennt die Zonen-Register
`Z<n>DayTemp` / `Z<n>RoomTemp` / `Z<n>OpMode` und bildet je **aktive** Zone ein
Thermostat ab: Ziel = Komfort-Sollwert (`Z<n>DayTemp`), Ist = `Z<n>RoomTemp`,
Modi aus `Z<n>OpMode` (off→OFF, auto→AUTO, sonst HEAT + Preset). Fehlen die
Register (Nicht-Vaillant), entsteht kein Gerät. Rohe Entitäten bleiben zusätzlich.

Zusätzlich (optional, siehe `EbusdBoilerClimate`): eine modulierende
Kessel-Regelung für BAI-Kreise ohne eigenen Raumregler (z. B. nach Ausbau
eines Exacontrol) -- ersetzt kein Zonenregister, sondern schreibt periodisch
einen selbst berechneten Vorlauf-Sollwert über `SetMode`.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

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
    CONF_BOILER_CURVE_SLOPE,
    CONF_BOILER_FLOW_MAX,
    CONF_BOILER_FLOW_MIN,
    CONF_BOILER_KI,
    CONF_BOILER_KP,
    CONF_BOILER_OUTDOOR_SENSOR,
    CONF_BOILER_ROOM_SENSOR,
    CONF_BOILER_WRITE_INTERVAL,
    DEFAULT_BOILER_CURVE_SLOPE,
    DEFAULT_BOILER_FLOW_MAX,
    DEFAULT_BOILER_FLOW_MIN,
    DEFAULT_BOILER_KI,
    DEFAULT_BOILER_KP,
    DEFAULT_BOILER_WRITE_INTERVAL,
    DOMAIN,
)
from .coordinator import EbusdCoordinator
from .entity import build_device_info
from .model import FieldDesc
from .regulation import RegulationParams, compute_flow_setpoint, format_setmode

_LOGGER = logging.getLogger(__name__)

_ROLE = {"DayTemp": "day", "RoomTemp": "room", "OpMode": "op"}
_ZONE_RE = re.compile(r"^Z(\d+)(DayTemp|RoomTemp|OpMode)$")

# Felder, die eine Kessel-SetMode-Regelung ermöglichen (siehe regulation.py).
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
            continue  # ohne schreibbaren Komfort-Sollwert kein Thermostat
        # nur aktive Zonen (mit tatsächlichen Daten) anlegen
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
    """Kessel-Regelung: nur anlegen, wenn Raum- UND Außensensor konfiguriert sind.

    Opt-in per Options-Flow -- ohne beide Sensoren gibt es keinen sinnvollen
    Sollwert zu berechnen, also lieber gar keine Entity als eine kaputte.
    """
    room_sensor = entry.options.get(CONF_BOILER_ROOM_SENSOR)
    outdoor_sensor = entry.options.get(CONF_BOILER_OUTDOOR_SENSOR)
    if not room_sensor or not outdoor_sensor:
        return []

    circuits: dict[str, set[str]] = {}
    for d in coordinator.fields:
        if d.message == "SetMode" and d.field in _SETMODE_FIELDS:
            circuits.setdefault(d.circuit, set()).add(d.field)

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
    )
    interval = int(
        entry.options.get(CONF_BOILER_WRITE_INTERVAL, DEFAULT_BOILER_WRITE_INTERVAL)
    )
    return [
        EbusdBoilerClimate(
            coordinator, circuit, room_sensor, outdoor_sensor, params, interval
        )
        for circuit, fields in sorted(circuits.items())
        if _SETMODE_FIELDS <= fields
    ]



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
        self._attr_name = f"Heizkreis {zone}"
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
            raise HomeAssistantError(f"Zonen-Write fehlgeschlagen: {err}") from err
        try:
            await self.coordinator.client.read(desc.circuit, desc.message)
        except EbusdError:
            pass
        await self.coordinator.async_request_refresh()


class EbusdBoilerClimate(CoordinatorEntity[EbusdCoordinator], RestoreEntity, ClimateEntity):
    """Modulierende Kessel-Regelung ohne eigenen Raumregler am Bus.

    Ersetzt ein außentemperaturgeführtes Raumthermostat (z. B. ein ausgebautes
    Exacontrol): `target_temperature` ist der Raum-Sollwert, `current_temperature`
    kommt von einem frei wählbaren externen Sensor. Statt nur ein/aus zu schalten,
    wird periodisch ein selbst berechneter Vorlauf-Sollwert (Heizkurve + Raum-PI,
    siehe `regulation.py`) über das eBUS-Kommando `SetMode` geschrieben --
    modulierend, wie es die BAI-Kesselelektronik selbst erwartet.

    Jeder Zyklus schreibt neu, unabhängig davon, ob sich etwas geändert hat --
    das dient zugleich als Lebenszeichen für den Kessel (Failsafe-Verhalten bei
    Ausfall von HA/ebusd liegt dann an dessen eigener Elektronik).
    """

    _attr_has_entity_name = True
    _attr_name = "Heating"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 10
    _attr_max_temp = 28
    _attr_target_temperature_step = 0.5
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
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
        outdoor_sensor: str,
        params: RegulationParams,
        write_interval: int,
    ) -> None:
        super().__init__(coordinator)
        self._circuit = circuit
        self._room_sensor = room_sensor
        self._outdoor_sensor = outdoor_sensor
        self._params = params
        self._write_interval = write_interval
        self._flame_key = (circuit, "Flame", "Flame")
        self._integral = 0.0
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
        await self._async_regulate()  # erster Zyklus sofort, nicht erst nach Intervall

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
        return {"regulation_integral": round(self._integral, 3)}

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        flame = self.coordinator.data.get(self._flame_key)
        if flame is None:
            return None
        return HVACAction.HEATING if str(flame).lower() == "on" else HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._attr_target_temperature = float(temp)
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

    async def _async_regulate(self, _now: Any = None) -> None:
        active = self.hvac_mode == HVACMode.HEAT
        flow_setpoint: float | None = None
        if active:
            self._update_current_temperature()
            outdoor = self._read_temperature(self._outdoor_sensor)
            if self._attr_current_temperature is None or outdoor is None:
                _LOGGER.warning(
                    "%s: Raum- oder Außensensor nicht verfügbar, Regelzyklus "
                    "übersprungen", self.entity_id,
                )
                return
            result = compute_flow_setpoint(
                target_room=self._attr_target_temperature,
                current_room=self._attr_current_temperature,
                outdoor_temp=outdoor,
                integral=self._integral,
                params=self._params,
            )
            self._integral = result.integral
            flow_setpoint = result.flow_setpoint
        value = format_setmode(flow_setpoint, active)
        try:
            await self.coordinator.client.write(self._circuit, "SetMode", value)
        except EbusdError as err:
            _LOGGER.warning(
                "%s: SetMode-Schreiben fehlgeschlagen: %s", self.entity_id, err
            )
            return
        self.async_write_ha_state()
