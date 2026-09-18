"""Persistent storage for the weekly schedule, per boiler circuit.

Separates the Home Assistant-specific storage API
(`homeassistant.helpers.storage.Store`) from the pure evaluation logic in
`schedule.py`, so the latter stays testable without an HA dependency.

One instance is created ONCE per boiler circuit (see `__init__.py`) and shared
between `climate.py` (reads) and `calendar.py` (writes/reads) via the
coordinator -- the same instance, so changes are immediately visible to both
(no reload from disk needed, no stale-cache risk).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .schedule import ScheduleEvent

_STORAGE_VERSION = 1


def _parse_hm(value: str) -> time:
    h, m = value.split(":")[:2]
    return time(int(h), int(m))


def _format_hm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


class HeatingScheduleStore:
    """Weekly schedule for one boiler circuit, persisted under `.storage/`."""

    def __init__(self, hass: HomeAssistant, entry_id: str, circuit: str) -> None:
        self._store: Store = Store(
            hass, _STORAGE_VERSION, f"{DOMAIN}_{entry_id}_{circuit}_heating_schedule"
        )
        self._events: list[ScheduleEvent] | None = None
        # climate.py and calendar.py share this instance and could both load
        # it for the first time simultaneously during setup -- the lock
        # prevents a duplicate (harmless but unnecessary) disk access.
        self._load_lock = asyncio.Lock()

    async def async_events(self) -> list[ScheduleEvent]:
        """Current events, from cache or freshly loaded from disk."""
        if self._events is None:
            async with self._load_lock:
                if self._events is None:  # may have been loaded while waiting
                    raw = await self._store.async_load() or []
                    self._events = [
                        ScheduleEvent(
                            id=item["id"],
                            weekday=item["weekday"],
                            start=_parse_hm(item["start"]),
                            end=_parse_hm(item["end"]),
                            temperature=float(item["temperature"]),
                        )
                        for item in raw
                    ]
        return self._events

    @property
    def cached_events(self) -> list[ScheduleEvent]:
        """Last-loaded events, readable synchronously (empty if never loaded yet).

        For `CalendarEntity.event`, which (unlike `async_get_events`) must be
        synchronous -- callers ensure via `async_events()` in
        `async_added_to_hass` that this has already been loaded.
        """
        return self._events if self._events is not None else []

    async def _async_save(self) -> None:
        await self._store.async_save(
            [
                {
                    "id": ev.id,
                    "weekday": ev.weekday,
                    "start": _format_hm(ev.start),
                    "end": _format_hm(ev.end),
                    "temperature": ev.temperature,
                }
                for ev in (self._events or [])
            ]
        )

    async def async_add(
        self, weekday: int, start: time, end: time, temperature: float
    ) -> ScheduleEvent:
        events = await self.async_events()
        event = ScheduleEvent(
            id=uuid.uuid4().hex, weekday=weekday, start=start, end=end,
            temperature=temperature,
        )
        events.append(event)
        await self._async_save()
        return event

    async def async_update(
        self, event_id: str, weekday: int, start: time, end: time, temperature: float
    ) -> None:
        events = await self.async_events()
        for i, ev in enumerate(events):
            if ev.id == event_id:
                events[i] = ScheduleEvent(
                    id=event_id, weekday=weekday, start=start, end=end,
                    temperature=temperature,
                )
                await self._async_save()
                return
        raise KeyError(f"Unknown schedule event ID: {event_id}")

    async def async_remove(self, event_id: str) -> None:
        events = await self.async_events()
        self._events = [ev for ev in events if ev.id != event_id]
        await self._async_save()
