"""Reine Logik für das Wochen-Zeitprogramm der Kessel-Regelung (climate.py).

Bewusst ohne Home-Assistant-Import (wie `regulation.py`), damit sie isoliert
und ohne die `homeassistant`-Abhängigkeit getestet werden kann (siehe
`tests/test_schedule.py`). Die Persistenz (Speichern/Laden über
`homeassistant.helpers.storage.Store`) lebt getrennt in `schedule_store.py`.
"""
from __future__ import annotations

from datetime import time
from typing import NamedTuple


class ScheduleEvent(NamedTuple):
    """Ein wiederkehrender Wochen-Slot.

    `weekday`: 0=Montag .. 6=Sonntag (wie `datetime.weekday()`).
    Ein über Mitternacht laufendes Fenster (z. B. 22:00-06:00) gehört zu dem
    Wochentag, an dem es BEGINNT (übliche Konvention für Nachtabsenkung).
    """

    id: str
    weekday: int
    start: time
    end: time
    temperature: float


def active_setpoint(
    events: list[ScheduleEvent], weekday: int, now: time
) -> float | None:
    """Soll-Temperatur des aktuell aktiven Slots, oder None wenn keiner passt.

    Bei Überlappungen gewinnt der zuerst in `events` gefundene Slot --
    Überlappungsfreiheit sicherzustellen ist Aufgabe der Kalender-UI
    (`calendar.py`), nicht dieser Funktion.
    """
    for ev in events:
        if ev.weekday != weekday:
            continue
        if ev.start <= ev.end:
            if ev.start <= now < ev.end:
                return ev.temperature
        # Über Mitternacht: nur der Abend-Teil (ab `start`) gehört zu diesem
        # Wochentag; der Morgen-Teil (vor `end`) gehört zum FOLGETAG und wird
        # unten über `prev_weekday` behandelt, wenn `weekday` == Folgetag ist.
        elif now >= ev.start:
            return ev.temperature
    prev_weekday = (weekday - 1) % 7
    for ev in events:
        if ev.weekday == prev_weekday and ev.start > ev.end and now < ev.end:
            return ev.temperature
    return None
