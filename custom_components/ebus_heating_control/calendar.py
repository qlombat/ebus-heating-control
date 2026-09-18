"""Calendar platform: Vaillant weekly schedules.

Read: one message per slot, `<prefix>Timer_<weekday><slot>` with `htm` (from),
`htm_1` (to), optionally `slottemp`. One calendar per (circuit, prefix).

Write (only when ebusd offers a writable day message `<prefix>Timer_<day>` --
otherwise read-only): ebusd's official timer convention is one write per day
with `slotIndex;slotCount;from;to[;temp]`. We pass that through via the TCP
`write`. Editing always applies to the **entire** weekly window (the device
has no per-day exception). `slotCount` semantics are best-effort and will be
finally verified on a system with writable timers.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import EbusdError
from .const import DOMAIN
from .coordinator import EbusdCoordinator
from .entity import build_device_info
from .schedule_store import HeatingScheduleStore

_WEEKDAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
_WEEKDAY = {name: idx for idx, name in enumerate(_WEEKDAY_NAMES)}
_TIMER_RE = re.compile(
    r"^(?P<prefix>.+?)Timer_(?P<day>" + "|".join(_WEEKDAY) + r")(?P<slot>\d+)$"
)


def _parse_hm(value: object) -> time | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        h, m = value.split(":")[:2]
        return time(int(h), int(m))
    except ValueError:
        return None


def _parse_temp(summary: str | None) -> float | None:
    if not summary:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", summary)
    return float(m.group().replace(",", ".")) if m else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EbusdCoordinator = hass.data[DOMAIN][entry.entry_id]
    writable_msgs = {d.message for d in coordinator.fields if d.writable}

    # (circuit, prefix) -> list of (weekday, slot, read_message)
    schedules: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
    has_temp: dict[tuple[str, str], bool] = {}
    seen: set[tuple[str, str]] = set()
    for d in coordinator.fields:
        m = _TIMER_RE.match(d.message)
        if not m:
            continue
        pkey = (d.circuit, m.group("prefix"))
        if d.field == "slottemp":  # this weekly schedule has a target temperature
            has_temp[pkey] = True
        if (d.circuit, d.message) in seen:
            continue
        seen.add((d.circuit, d.message))
        schedules.setdefault(pkey, []).append(
            (_WEEKDAY[m.group("day")], int(m.group("slot")), d.message)
        )

    entities: list[Any] = []
    for (circuit, prefix), slots in schedules.items():
        # writable if the day write message exists for one of the days
        writable = any(
            f"{prefix}Timer_{_WEEKDAY_NAMES[wd]}" in writable_msgs
            for wd, _, _ in slots
        )
        entities.append(
            EbusdCalendar(
                coordinator, circuit, prefix, slots,
                writable=writable, has_temp=has_temp.get((circuit, prefix), False),
            )
        )
    # Weekly schedule of the HA-side boiler regulation (see climate.py /
    # schedule.py) -- one calendar per circuit with a configured store.
    for circuit, store in coordinator.heating_schedule_stores.items():
        entities.append(EbusdHeatingScheduleCalendar(coordinator, circuit, store))
    async_add_entities(entities)


class EbusdCalendar(CoordinatorEntity[EbusdCoordinator], CalendarEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbusdCoordinator,
        circuit: str,
        prefix: str,
        slots: list[tuple[int, int, str]],
        writable: bool,
        has_temp: bool,
    ) -> None:
        super().__init__(coordinator)
        self._circuit = circuit
        self._prefix = prefix
        self._has_temp = has_temp
        # weekday -> sorted list of (slot, read_message)
        self._by_day: dict[int, list[tuple[int, str]]] = {}
        for weekday, slot, msg in slots:
            self._by_day.setdefault(weekday, []).append((slot, msg))
        for day_slots in self._by_day.values():
            day_slots.sort()
        self._attr_name = f"{prefix} schedule"
        self._attr_unique_id = f"{DOMAIN}_{circuit}_{prefix}_timer".lower()
        if writable:
            self._attr_supported_features = (
                CalendarEntityFeature.CREATE_EVENT
                | CalendarEntityFeature.UPDATE_EVENT
                | CalendarEntityFeature.DELETE_EVENT
            )
        self._attr_device_info = build_device_info(coordinator, circuit)

    # ---- Read ----------------------------------------------------------------
    def _slot_value(self, read_msg: str) -> tuple[time | None, time | None, object]:
        get = self.coordinator.data.get
        frm = _parse_hm(get((self._circuit, read_msg, "htm")))
        to = _parse_hm(get((self._circuit, read_msg, "htm_1")))
        temp = get((self._circuit, read_msg, "slottemp"))
        return frm, to, temp

    def _uid(self, weekday: int, slot: int, day: date) -> str:
        return f"{self._circuit}|{self._prefix}|{weekday}|{slot}|{day.isoformat()}"

    def _events_for_day(self, day: date) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        tz = dt_util.get_default_time_zone()
        for slot, msg in self._by_day.get(day.weekday(), []):
            frm, to, temp = self._slot_value(msg)
            if frm is None or to is None or frm == to:
                continue  # empty/invalid slot
            start = datetime.combine(day, frm, tzinfo=tz)
            end = datetime.combine(day, to, tzinfo=tz)
            if end <= start:
                end += timedelta(days=1)  # crosses midnight
            try:
                summary = f"{float(temp):g} °C"
            except (TypeError, ValueError):
                summary = "on"
            events.append(
                CalendarEvent(
                    start=start, end=end, summary=summary,
                    uid=self._uid(day.weekday(), slot, day),
                )
            )
        return events

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        day = start_date.date()
        while day <= end_date.date():
            events.extend(self._events_for_day(day))
            day += timedelta(days=1)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        upcoming: list[CalendarEvent] = []
        for offset in range(8):
            upcoming.extend(self._events_for_day((now + timedelta(days=offset)).date()))
        upcoming.sort(key=lambda e: e.start)
        for ev in upcoming:
            if ev.end > now:
                return ev
        return None

    # ---- Write -----------------------------------------------------------
    def _active_slots(self, weekday: int) -> int:
        count = 0
        for _slot, msg in self._by_day.get(weekday, []):
            frm, to, _ = self._slot_value(msg)
            if frm is not None and to is not None and frm != to:
                count += 1
        return count

    async def _write_slot(
        self,
        weekday: int,
        slot: int,
        frm: str,
        to: str,
        temp: float | None,
        count: int,
    ) -> None:
        msg = f"{self._prefix}Timer_{_WEEKDAY_NAMES[weekday]}"
        parts = [str(slot), str(max(count, 0)), frm, to]
        if self._has_temp:
            parts.append(f"{float(temp if temp is not None else 0):g}")
        value = ";".join(parts)
        try:
            await self.coordinator.client.write(self._circuit, msg, value)
        except EbusdError as err:
            raise HomeAssistantError(f"Timer write failed: {err}") from err
        # freshly read the affected read slots, so the calendar is up to date
        for _slot, read_msg in self._by_day.get(weekday, []):
            try:
                await self.coordinator.client.read(self._circuit, read_msg)
            except EbusdError:
                pass
        await self.coordinator.async_request_refresh()

    @staticmethod
    def _hm(value: datetime) -> str:
        return dt_util.as_local(value).strftime("%H:%M")

    def _decode_uid(self, uid: str) -> tuple[int, int]:
        try:
            parts = uid.split("|")
            return int(parts[2]), int(parts[3])
        except (IndexError, ValueError) as err:
            raise HomeAssistantError(f"Invalid event ID: {uid}") from err

    async def async_create_event(self, **kwargs: Any) -> None:
        start = kwargs.get("dtstart")
        end = kwargs.get("dtend")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise HomeAssistantError("Schedule windows need a time of day.")
        weekday = dt_util.as_local(start).weekday()
        # find the first empty slot of the day
        free = None
        for slot, msg in self._by_day.get(weekday, []):
            frm, to, _ = self._slot_value(msg)
            if frm is None or frm == to:
                free = slot
                break
        if free is None:
            raise HomeAssistantError("All slots for this weekday are taken.")
        await self._write_slot(
            weekday, free, self._hm(start), self._hm(end),
            _parse_temp(kwargs.get("summary")), self._active_slots(weekday) + 1,
        )

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        weekday, slot = self._decode_uid(uid)
        start = event.get("dtstart")
        end = event.get("dtend")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise HomeAssistantError("Schedule windows need a time of day.")
        await self._write_slot(
            weekday, slot, self._hm(start), self._hm(end),
            _parse_temp(event.get("summary")), self._active_slots(weekday),
        )

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        weekday, slot = self._decode_uid(uid)
        # Clear the slot: window to 00:00-00:00, decrement slot count
        await self._write_slot(
            weekday, slot, "00:00", "00:00", 0,
            max(self._active_slots(weekday) - 1, 0),
        )


class EbusdHeatingScheduleCalendar(CoordinatorEntity[EbusdCoordinator], CalendarEntity):
    """Weekly schedule for the HA-side boiler regulation (climate.py).

    Stored purely on the HA side (see `schedule_store.py`) -- unlike
    `EbusdCalendar` above, no eBUS messages, so always fully editable (no
    fixed slot budget per weekday). Every event has a stable ID of its own,
    title = target temperature (e.g. "21 °C"), valid for the weekday it
    starts on (windows crossing midnight, see `schedule.active_setpoint`).
    """

    _attr_has_entity_name = True
    _attr_name = "Heating schedule"
    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )

    def __init__(
        self, coordinator: EbusdCoordinator, circuit: str, store: HeatingScheduleStore
    ) -> None:
        super().__init__(coordinator)
        self._circuit = circuit
        self._store = store
        self._attr_unique_id = f"{DOMAIN}_{circuit}_heating_schedule".lower()
        self._attr_device_info = build_device_info(coordinator, circuit)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Load once, so the synchronous `event` property can use the
        # cache afterwards (see `HeatingScheduleStore.cached_events`).
        await self._store.async_events()

    def _events_for_day(self, day: date) -> list[CalendarEvent]:
        tz = dt_util.get_default_time_zone()
        out: list[CalendarEvent] = []
        for ev in self._store.cached_events:
            if ev.weekday != day.weekday() or ev.start == ev.end:
                continue  # different weekday, or empty/invalid slot
            start_dt = datetime.combine(day, ev.start, tzinfo=tz)
            end_dt = datetime.combine(day, ev.end, tzinfo=tz)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)  # crosses midnight
            out.append(
                CalendarEvent(
                    start=start_dt, end=end_dt,
                    summary=f"{ev.temperature:g} °C", uid=ev.id,
                )
            )
        return out

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        await self._store.async_events()
        events: list[CalendarEvent] = []
        day = start_date.date()
        while day <= end_date.date():
            events.extend(self._events_for_day(day))
            day += timedelta(days=1)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        upcoming: list[CalendarEvent] = []
        for offset in range(8):
            upcoming.extend(self._events_for_day((now + timedelta(days=offset)).date()))
        upcoming.sort(key=lambda e: e.start)
        for ev in upcoming:
            if ev.end > now:
                return ev
        return None

    async def async_create_event(self, **kwargs: Any) -> None:
        start = kwargs.get("dtstart")
        end = kwargs.get("dtend")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise HomeAssistantError("Schedule windows need a time of day.")
        temp = _parse_temp(kwargs.get("summary"))
        if temp is None:
            raise HomeAssistantError(
                'Title must contain the target temperature (e.g. "21 °C").'
            )
        weekday = dt_util.as_local(start).weekday()
        await self._store.async_add(
            weekday, dt_util.as_local(start).time(), dt_util.as_local(end).time(), temp,
        )
        self.async_write_ha_state()

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        start = event.get("dtstart")
        end = event.get("dtend")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise HomeAssistantError("Schedule windows need a time of day.")
        temp = _parse_temp(event.get("summary"))
        if temp is None:
            raise HomeAssistantError(
                'Title must contain the target temperature (e.g. "21 °C").'
            )
        weekday = dt_util.as_local(start).weekday()
        try:
            await self._store.async_update(
                uid, weekday,
                dt_util.as_local(start).time(), dt_util.as_local(end).time(), temp,
            )
        except KeyError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        await self._store.async_remove(uid)
        self.async_write_ha_state()
