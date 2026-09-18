"""Unit tests for regulation.py (HA-free, loaded directly by path)."""
import importlib.util
import sys
from pathlib import Path

_REGULATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "ebus_heating_control" / "regulation.py"
)
_spec = importlib.util.spec_from_file_location("ebus_regulation", _REGULATION_PATH)
regulation = importlib.util.module_from_spec(_spec)
# dataclasses resolves type annotations via sys.modules[cls.__module__] --
# the module must be registered there for that (like with a regular import).
sys.modules[_spec.name] = regulation
_spec.loader.exec_module(regulation)


def _params(**overrides):
    base = dict(curve_slope=1.2, kp=3.0, ki=0.1, flow_min=30.0, flow_max=55.0)
    base.update(overrides)
    return regulation.RegulationParams(**base)


def test_curve_only_when_room_at_target():
    # No room error -> only the heating curve applies (Kp/integral contribute nothing).
    result = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=20.0, outdoor_temp=5.0,
        integral=0.0, params=_params(),
    )
    assert result.error == 0.0
    assert result.flow_setpoint == 20.0 + 1.2 * (20.0 - 5.0)
    assert result.integral == 0.0


def test_no_outdoor_sensor_falls_back_to_base_flow():
    # outdoor_temp=None (no sensor configured) -> heating curve is skipped,
    # base_flow is the fixed base, only the room PI still applies.
    params = _params(base_flow=35.0, ki=0.0)  # ki=0: pure P term, easy to recompute
    result = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=20.0, outdoor_temp=None,
        integral=0.0, params=params,
    )
    assert result.flow_setpoint == 35.0
    warmer = regulation.compute_flow_setpoint(
        target_room=20.0, current_room=19.0, outdoor_temp=None,
        integral=0.0, params=params,
    )
    assert warmer.flow_setpoint == 35.0 + params.kp * 1.0


def test_positive_error_raises_flow_setpoint():
    # Room too cold (error > 0) -> flow should rise.
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
    assert result.integral == 2.0  # clamped, not 5 * 10 = 50


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


def test_format_setmode_calling_for_heat_sets_hcmode_auto_and_flow():
    assert (
        regulation.format_setmode(30.0, calling_for_heat=True)
        == "auto;30.0;-;-;0;0;0;0;0;0"
    )


def test_format_setmode_not_calling_for_heat_sets_disablehc_keeps_hcmode_auto():
    # Regardless of user-off or hysteresis-satisfied: hcmode ALWAYS stays
    # "auto" (DHW stays independently functional via HwcSwitch/
    # hwctempdesired), only the granular disablehc bit blocks heating.
    assert (
        regulation.format_setmode(30.0, calling_for_heat=False)
        == "auto;-;-;-;1;0;0;0;0;0"
    )
    assert (
        regulation.format_setmode(None, calling_for_heat=True)
        == "auto;-;-;-;1;0;0;0;0;0"
    )


def test_should_call_for_heat_starts_when_room_below_target():
    assert regulation.should_call_for_heat(
        target_room=20.0, current_room=19.9, currently_calling=False, hysteresis=0.3
    )
    assert not regulation.should_call_for_heat(
        target_room=20.0, current_room=20.0, currently_calling=False, hysteresis=0.3
    )


def test_should_call_for_heat_keeps_running_until_hysteresis_exceeded():
    # Already heating, room slightly over setpoint -> don't turn off yet.
    assert regulation.should_call_for_heat(
        target_room=20.0, current_room=20.2, currently_calling=True, hysteresis=0.3
    )
    # Only turn off once the hysteresis is exceeded.
    assert not regulation.should_call_for_heat(
        target_room=20.0, current_room=20.4, currently_calling=True, hysteresis=0.3
    )


def test_should_call_for_heat_no_short_cycling_at_exact_target():
    # A plain float comparison without hysteresis would chatter exactly at
    # the target; with currently_calling=True it keeps "still heating" until
    # past the hysteresis.
    assert regulation.should_call_for_heat(
        target_room=20.0, current_room=20.0, currently_calling=True, hysteresis=0.3
    )
