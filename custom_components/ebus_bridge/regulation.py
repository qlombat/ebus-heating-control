"""Reine Regelungslogik für die modulierende Kessel-Vorlaufsollwert-Berechnung.

Bewusst ohne jeden Home-Assistant-Import (wie `model.py`), damit sie isoliert
und ohne die `homeassistant`-Abhängigkeit getestet werden kann (siehe
`tests/test_regulation.py`).

Bildet nach, was ein außentemperaturgeführtes Raumthermostat (z. B. das
ersetzte Exacontrol) tut, aber transparent und einstellbar:

    Vorlauf-Basis (Heizkurve) = Raum-Soll + Steigung * (Raum-Soll - Außentemp)
    Korrektur (PI)            = Kp * Raumfehler + Integral
    Vorlauf-Sollwert          = clamp(Basis + Korrektur, flow_min, flow_max)

Der Vorlauf-Sollwert wird anschließend über die eBUS-Kommando `SetMode` des
BAI-Kreises geschrieben (siehe `format_setmode`); das ist der einzige Weg,
`flowtempdesired` modulierend zu setzen (Einzelfeld-Schreiben ist bei
Mehrfeld-Kommandos wie `SetMode` von ebusd nicht vorgesehen).
"""
from __future__ import annotations

from dataclasses import dataclass

# SetMode kennt laut Vaillant-Protokoll (hcmode-Template) 0=auto;1=off;2=heat;3=water.
# "auto" lässt die Kesselelektronik selbst entscheiden, ob Heizung oder WW bedient
# wird, respektiert aber den mitgeschickten Vorlauf-Sollwert -- am echten Gerät
# validiert (FlowTempDesired 60 °C -> 30 °C). "off" schaltet die Heizfunktion ab,
# ohne die WW-Bereitung (HwcSwitch) zu beeinflussen.
HCMODE_ACTIVE = "auto"
HCMODE_OFF = "off"


@dataclass(frozen=True)
class RegulationParams:
    """Einstellbare Regelparameter (kommen aus dem Options-Flow)."""

    curve_slope: float
    kp: float
    ki: float
    flow_min: float
    flow_max: float
    # Begrenzung des Integralanteils allein (Anti-Windup) -- unabhängig von
    # flow_min/flow_max, damit ein bereits gesättigter Vorlauf den Integral-
    # Zustand nicht unbegrenzt weiter aufsummiert.
    integral_limit: float = 10.0
    # Hysterese der Wärmeanforderung (°C): erst AUS, wenn der Raum die Konsigne
    # um mehr als diesen Wert überschreitet (siehe `should_call_for_heat`).
    # Ohne das würde die Kesselregelung nie wirklich stoppen -- die Heizkurve
    # allein liefert auch bei erreichter Konsigne einen Vorlauf > 0.
    hysteresis: float = 0.3


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
    """Hysterese-Wärmeanforderung: verhindert Kurzzyklen um die Konsigne.

    Startet die Anforderung sobald der Raum unter die Konsigne fällt, beendet
    sie erst, wenn er sie um `hysteresis` überschreitet -- ohne diese Sperre
    würde `compute_flow_setpoint` (reine Heizkurve) auch bei erreichter
    Konsigne dauerhaft einen Vorlauf > 0 liefern und der Kessel liefe endlos.
    """
    if currently_calling:
        return current_room < target_room + hysteresis
    return current_room < target_room


def compute_flow_setpoint(
    target_room: float,
    current_room: float,
    outdoor_temp: float,
    integral: float,
    params: RegulationParams,
) -> RegulationResult:
    """Berechnet Vorlauf-Sollwert und neuen Integral-Zustand für einen Zyklus.

    Rein und seiteneffektfrei: `integral` kommt vom Aufrufer (z. B. über
    `RestoreEntity` persistiert) und wird als Teil des Ergebnisses zurückgegeben,
    statt hier gespeichert zu werden.
    """
    error = target_room - current_room
    curve_base = target_room + params.curve_slope * (target_room - outdoor_temp)
    integral = max(
        -params.integral_limit,
        min(params.integral_limit, integral + error * params.ki),
    )
    raw_flow = curve_base + params.kp * error + integral
    flow = max(params.flow_min, min(params.flow_max, raw_flow))
    return RegulationResult(flow_setpoint=round(flow, 1), integral=round(integral, 3), error=error)


def format_setmode(flow_setpoint: float | None, active: bool, calling_for_heat: bool = True) -> str:
    """Baut den 10-Felder-Wertestring für das eBUS-Kommando `SetMode`.

    Drei Zustände:
    - `active=False` (Nutzer hat HVACMode.OFF gewählt): `hcmode=off`, Kessel
      komplett abgeschaltet (auch WW).
    - `active=True, calling_for_heat=True`: normale Modulation, `hcmode=auto`
      mit dem berechneten Vorlauf-Sollwert, `disablehc=0`.
    - `active=True, calling_for_heat=False`: Konsigne erreicht (Hysterese, siehe
      `should_call_for_heat`) -- `hcmode=auto` bleibt (WW weiter erlaubt), aber
      `disablehc=1` sperrt die Heizfunktion, statt dauerhaft weiter zu modulieren.

    Nur `hcmode`, `flowtempdesired` und `disablehc` werden gesetzt (`-` = un-
    verändert lassen); die restlichen Felder bleiben 0 (keine weiteren Sperren/
    Freigaben angefordert) -- entspricht dem am realen Gerät validierten
    Testwert `auto;30;-;-;0;0;0;0;0;0`.
    """
    if not active or flow_setpoint is None:
        return f"{HCMODE_OFF};-;-;-;0;0;0;0;0;0"
    disablehc = 0 if calling_for_heat else 1
    return f"{HCMODE_ACTIVE};{flow_setpoint};-;-;{disablehc};0;0;0;0;0"
