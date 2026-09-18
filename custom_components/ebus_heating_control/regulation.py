"""Pure regulation logic for the modulating boiler flow-setpoint computation.

Deliberately free of any Home Assistant import (like `model.py`), so it can be
tested in isolation without the `homeassistant` dependency (see
`tests/test_regulation.py`).

Reproduces what an outdoor-temperature-compensated room thermostat (e.g. the
replaced Exacontrol) does, but transparently and configurably:

    Base flow (heating curve) = room target + slope * (room target - outdoor temp)
    Correction (PI)           = Kp * room error + integral
    Flow setpoint             = clamp(base + correction, flow_min, flow_max)

The flow setpoint is then written via the BAI circuit's `SetMode` eBUS command
(see `format_setmode`); that's the only way to set `flowtempdesired` in a
modulating fashion (ebusd does not support single-field writes for multi-field
commands like `SetMode`).
"""
from __future__ import annotations

from dataclasses import dataclass

# Per the Vaillant protocol (hcmode template), SetMode knows 0=auto;1=off;2=heat;3=water.
# "auto" lets the boiler electronics decide themselves whether heating or DHW is
# served, but still honors the supplied flow setpoint -- validated on the real
# device (FlowTempDesired 60 °C -> 30 °C). Deliberately NEVER "off": that's the
# coarse overall boiler mode and likely also disables DHW production, which
# defeats the goal of keeping DHW independent from this entity's heating
# on/off state. Heating on/off instead runs exclusively through the granular,
# unambiguously named `disablehc` bit (see `format_setmode`).
HCMODE_ACTIVE = "auto"


@dataclass(frozen=True)
class RegulationParams:
    """Configurable regulation parameters (come from the options flow)."""

    curve_slope: float
    kp: float
    ki: float
    flow_min: float
    flow_max: float
    # Limits the integral term alone (anti-windup) -- independent of
    # flow_min/flow_max, so that an already-saturated flow doesn't keep
    # accumulating the integral state without bound.
    integral_limit: float = 10.0
    # Heat-demand hysteresis (°C): only turns OFF once the room exceeds the
    # setpoint by more than this value (see `should_call_for_heat`).
    # Without this, the boiler regulation would never really stop -- the
    # heating curve alone still returns a flow > 0 even once setpoint is reached.
    hysteresis: float = 0.3
    # Base flow temperature when no outdoor sensor is configured (the heating
    # curve is then skipped entirely -- pure room-PI regulation around this
    # base value).
    base_flow: float = 35.0


@dataclass(frozen=True)
class RegulationResult:
    flow_setpoint: float
    integral: float
    error: float


def should_call_for_heat(
    target_room: float,
    current_room: float,
    currently_calling: bool,
    hysteresis: float,
) -> bool:
    """Hysteresis heat demand: prevents short-cycling around the setpoint.

    Starts the demand as soon as the room drops below the setpoint, only ends
    it once the room exceeds it by `hysteresis` -- without this guard,
    `compute_flow_setpoint` (pure heating curve) would keep returning a flow
    > 0 even once setpoint is reached, and the boiler would run indefinitely.
    """
    if currently_calling:
        return current_room < target_room + hysteresis
    return current_room < target_room


def compute_flow_setpoint(
    target_room: float,
    current_room: float,
    outdoor_temp: float | None,
    integral: float,
    params: RegulationParams,
) -> RegulationResult:
    """Compute the flow setpoint and new integral state for one cycle.

    `outdoor_temp=None` (no outdoor sensor configured): the heating curve is
    skipped, `params.base_flow` serves as a fixed starting point -- pure
    room-PI regulation. Less anticipatory than with weather compensation, but
    still works without a second sensor.

    Pure and side-effect-free: `integral` comes from the caller (e.g.
    persisted via `RestoreEntity`) and is returned as part of the result
    instead of being stored here.
    """
    error = target_room - current_room
    if outdoor_temp is None:
        curve_base = params.base_flow
    else:
        curve_base = target_room + params.curve_slope * (target_room - outdoor_temp)
    integral = max(
        -params.integral_limit,
        min(params.integral_limit, integral + error * params.ki),
    )
    raw_flow = curve_base + params.kp * error + integral
    flow = max(params.flow_min, min(params.flow_max, raw_flow))
    return RegulationResult(flow_setpoint=round(flow, 1), integral=round(integral, 3), error=error)


def format_setmode(flow_setpoint: float | None, calling_for_heat: bool) -> str:
    """Build the 10-field value string for the eBUS `SetMode` command.

    `hcmode` ALWAYS stays `"auto"` -- even when the user has turned heating
    off via this entity or the setpoint is reached (hysteresis, see
    `should_call_for_heat`). Only the granular `disablehc` bit blocks the
    heating function in that case; domestic hot water production
    (`HwcSwitch`, `hwctempdesired`) is left completely untouched. `hcmode="off"`
    would instead switch the boiler's entire mode and likely also disable DHW
    production -- exactly what this entity must not do.

    Only `hcmode`, `flowtempdesired` and `disablehc` are set (`-` = leave
    unchanged); the remaining fields stay 0 (no further locks/releases
    requested) -- matches the test value validated on the real device,
    `auto;30;-;-;0;0;0;0;0;0`.
    """
    if not calling_for_heat or flow_setpoint is None:
        return f"{HCMODE_ACTIVE};-;-;-;1;0;0;0;0;0"
    return f"{HCMODE_ACTIVE};{flow_setpoint};-;-;0;0;0;0;0;0"
