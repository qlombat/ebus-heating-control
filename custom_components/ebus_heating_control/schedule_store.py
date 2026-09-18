"""Persistente Speicherung des Wochen-Zeitprogramms je Kessel-Kreis.

Trennt die Home-Assistant-spezifische Speicher-API
(`homeassistant.helpers.storage.Store`) von der reinen Auswertungslogik in
`schedule.py`, damit letztere ohne HA-Abhängigkeit testbar bleibt.

Eine Instanz wird pro Kessel-Kreis EINMAL angelegt (siehe `__init__.py`) und
zwischen `climate.py` (liest) und `calendar.py` (schreibt/liest) über den
Coordinator geteilt -- dieselbe Instanz, damit Änderungen sofort für beide
sichtbar sind (kein Neuladen von der Platte nötig, kein Stale-Cache-Risiko).
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
    """Wochen-Zeitprogramm für einen Kessel-Kreis, persistiert unter `.storage/`."""

    def __init__(self, hass: HomeAssistant, entry_id: str, circuit: str) -> None:
        self._store: Store = Store(
            hass, _STORAGE_VERSION, f"{DOMAIN}_{entry_id}_{circuit}_heating_schedule"
        )
        self._events: list[ScheduleEvent] | None = None
        # climate.py und calendar.py teilen sich diese Instanz und könnten beim
        # Setup gleichzeitig zum ersten Mal laden -- Lock verhindert einen
        # doppelten (harmlosen, aber unnötigen) Platten-Zugriff.
        self._load_lock = asyncio.Lock()

    async def async_events(self) -> list[ScheduleEvent]:
        """Aktuelle Ereignisse, aus dem Cache oder frisch von der Platte geladen."""
        if self._events is None:
            async with self._load_lock:
                if self._events is None:  # evtl. während des Wartens geladen
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
        """Zuletzt geladene Ereignisse, synchron lesbar (leer, falls noch nie geladen).

        Für `CalendarEntity.event`, das (anders als `async_get_events`) synchron
        sein muss -- Aufrufer stellen per `async_events()` in `async_added_to_hass`
        sicher, dass hier schon geladen wurde.
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
        raise KeyError(f"Unbekannte Zeitprogramm-ID: {event_id}")

    async def async_remove(self, event_id: str) -> None:
        events = await self.async_events()
        self._events = [ev for ev in events if ev.id != event_id]
        await self._async_save()
