"""Pure logic for the weekly schedule of the boiler regulation (climate.py).

Deliberately free of any Home Assistant import (like `regulation.py`), so it
can be tested in isolation without the `homeassistant` dependency (see
`tests/test_schedule.py`). Persistence (saving/loading via
`homeassistant.helpers.storage.Store`) lives separately in `schedule_store.py`.
"""
from __future__ import annotations

from datetime import time
from typing import NamedTuple


class ScheduleEvent(NamedTuple):
    """A recurring weekly slot.

    `weekday`: 0=Monday .. 6=Sunday (like `datetime.weekday()`).
    A window crossing midnight (e.g. 22:00-06:00) belongs to the weekday it
    STARTS on (the usual convention for a night setback).
    """

    id: str
    weekday: int
    start: time
    end: time
    temperature: float


def active_setpoint(
    events: list[ScheduleEvent], weekday: int, now: time
) -> float | None:
    """Target temperature of the currently active slot, or None if none matches.

    On overlaps, the slot found first in `events` wins -- ensuring no overlap
    is the job of the calendar UI (`calendar.py`), not this function.
    """
    for ev in events:
        if ev.weekday != weekday:
            continue
        if ev.start <= ev.end:
            if ev.start <= now < ev.end:
                return ev.temperature
        # Crossing midnight: only the evening part (from `start`) belongs to
        # this weekday; the morning part (before `end`) belongs to the NEXT
        # DAY and is handled below via `prev_weekday`, when `weekday` ==
        # that next day.
        elif now >= ev.start:
            return ev.temperature
    prev_weekday = (weekday - 1) % 7
    for ev in events:
        if ev.weekday == prev_weekday and ev.start > ev.end and now < ev.end:
            return ev.temperature
    return None
