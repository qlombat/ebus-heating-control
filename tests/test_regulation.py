"""Unit-Tests für regulation.py (HA-frei, direkt per Pfad geladen)."""
import importlib.util
import sys
from pathlib import Path

_REGULATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "ebus_bridge" / "regulation.py"
)
_spec = importlib.util.spec_from_file_location("ebus_regulation", _REGULATION_PATH)
regulation = importlib.util.module_from_spec(_spec)
# dataclasses löst Typannotationen über sys.modules[cls.__module__] auf --
# das Modul muss dafür (wie beim regulären Import) dort registriert sein.
sys.modules[_spec.name] = regulation
_spec.loader.exec_module(regulation)


def _params(**overrides):
    base = dict(curve_slope=1.2, kp=3.0, ki=0.1, flow_min=30.0, flow_max=55.0)
    base.update(overrides)
    return regulation.RegulationParams(**base)


def test_curve_only_when_room_at_target():
    # Kein Raumfehler -> nur die Heizkurve wirkt (Kp/Integral tragen nichts bei).
    result = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=20.0, outdoor_temp=5.0,
        integral=0.0, params=_params(),
    )
    assert result.error == 0.0
    assert result.flow_setpoint == 20.0 + 1.2 * (20.0 - 5.0)
    assert result.integral == 0.0


def test_positive_error_raises_flow_setpoint():
    # Raum zu kalt (Fehler > 0) -> Vorlauf soll steigen.
    baseline = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=20.0, outdoor_temp=5.0,
        integral=0.0, params=_params(),
    ).flow_setpoint
    warmer = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=19.0, outdoor_temp=5.0,
        integral=0.0, params=_params(),
    ).flow_setpoint
    assert warmer > baseline


def test_integral_accumulates_across_cycles():
    params = _params()
    first = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=19.0, outdoor_temp=5.0,
        integral=0.0, params=params,
    )
    second = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=19.0, outdoor_temp=5.0,
        integral=first.integral, params=params,
    )
    assert second.integral > first.integral > 0.0


def test_integral_clamped_to_limit_antiwindup():
    params = _params(ki=5.0, integral_limit=2.0)
    result = regulation.compute_flow_setpoint(
        target_room=25.0, current_room=15.0, outdoor_temp=0.0,
        integral=0.0, params=params,
    )
    assert result.integral == 2.0  # geklemmt, nicht 5 * 10 = 50


def test_flow_setpoint_clamped_to_bounds():
    params = _params(flow_min=30.0, flow_max=55.0)
    too_hot = regulation.compute_flow_setpoint(
        target_room=25.0, current_room=10.0, outdoor_temp=-15.0,
        integral=0.0, params=params,
    )
    assert too_hot.flow_setpoint == 55.0
    too_cold = regulation.compute_flow_setpoint(
        target_room=16.0, current_room=22.0, outdoor_temp=18.0,
        integral=0.0, params=params,
    )
    assert too_cold.flow_setpoint == 30.0


def test_format_setmode_active_sets_hcmode_auto_and_flow():
    assert (
        regulation.format_setmode(30.0, active=True)
        == "auto;30.0;-;-;0;0;0;0;0;0"
    )


def test_format_setmode_inactive_sets_hcmode_off():
    assert regulation.format_setmode(30.0, active=False) == "off;-;-;-;0;0;0;0;0;0"
    assert regulation.format_setmode(None, active=True) == "off;-;-;-;0;0;0;0;0;0"
