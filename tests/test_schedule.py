"""Unit-Tests für schedule.py (HA-frei, direkt per Pfad geladen)."""
import importlib.util
import sys
from datetime import time
from pathlib import Path

_SCHEDULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "ebus_bridge" / "schedule.py"
)
_spec = importlib.util.spec_from_file_location("ebus_schedule", _SCHEDULE_PATH)
schedule = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = schedule
_spec.loader.exec_module(schedule)


def _ev(weekday, start, end, temp, id_="e"):
    return schedule.ScheduleEvent(
        id=id_, weekday=weekday, start=time(*start), end=time(*end), temperature=temp,
    )


def test_no_events_returns_none():
    assert schedule.active_setpoint([], weekday=0, now=time(12, 0)) is None


def test_simple_same_day_window_matches():
    events = [_ev(0, (6, 30), (22, 0), 21.0)]
    assert schedule.active_setpoint(events, weekday=0, now=time(7, 0)) == 21.0
    assert schedule.active_setpoint(events, weekday=0, now=time(23, 0)) is None
    assert schedule.active_setpoint(events, weekday=0, now=time(6, 0)) is None  # vor Start


def test_window_end_is_exclusive():
    events = [_ev(0, (6, 0), (22, 0), 21.0)]
    assert schedule.active_setpoint(events, weekday=0, now=time(22, 0)) is None


def test_different_weekday_does_not_match():
    events = [_ev(0, (6, 0), (22, 0), 21.0)]
    assert schedule.active_setpoint(events, weekday=1, now=time(12, 0)) is None


def test_zero_length_window_never_matches():
    events = [_ev(0, (6, 0), (6, 0), 21.0)]
    assert schedule.active_setpoint(events, weekday=0, now=time(6, 0)) is None


def test_overnight_window_evening_portion_same_weekday():
    # Samstag 22:00 -> Sonntag 06:00 (Nachtabsenkung)
    events = [_ev(5, (22, 0), (6, 0), 17.0)]
    assert schedule.active_setpoint(events, weekday=5, now=time(23, 0)) == 17.0


def test_overnight_window_morning_portion_next_weekday():
    events = [_ev(5, (22, 0), (6, 0), 17.0)]
    assert schedule.active_setpoint(events, weekday=6, now=time(2, 0)) == 17.0


def test_overnight_window_does_not_leak_into_same_day_morning():
    # Samstag 02:00 (VOR dem 22:00-06:00-Fenster desselben Wochentags) darf
    # nicht fälschlich als "noch aktiv" gelten -- das Fenster hat an diesem
    # Wochentag ja noch gar nicht begonnen.
    events = [_ev(5, (22, 0), (6, 0), 17.0)]
    assert schedule.active_setpoint(events, weekday=5, now=time(2, 0)) is None


def test_overnight_window_ends_before_next_day_end_time():
    events = [_ev(5, (22, 0), (6, 0), 17.0)]
    assert schedule.active_setpoint(events, weekday=6, now=time(6, 0)) is None
    assert schedule.active_setpoint(events, weekday=6, now=time(7, 0)) is None


def test_first_matching_event_wins_on_overlap():
    events = [
        _ev(0, (6, 0), (22, 0), 21.0, id_="a"),
        _ev(0, (8, 0), (20, 0), 19.0, id_="b"),
    ]
    assert schedule.active_setpoint(events, weekday=0, now=time(9, 0)) == 21.0


def test_multiple_days_only_matching_weekday_considered():
    events = [
        _ev(0, (6, 0), (22, 0), 21.0),
        _ev(1, (6, 0), (22, 0), 18.0),
    ]
    assert schedule.active_setpoint(events, weekday=1, now=time(12, 0)) == 18.0
